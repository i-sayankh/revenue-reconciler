"""Tests for backend/app/engine/reconcile.py.

Feeds the Step 5a fixtures (mini_orders.csv / mini_payments.csv) through
the parsing module, then the engine, and asserts against the exact
expected-results table in the Step 6 plan: every FIX-10xx order's exact
discrepancy type, the aggregate counts/totals, and the money-at-risk
formula (also unit-tested directly against hand-built discrepancies).
"""

from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.reconcile import (
    AMOUNT_TOLERANCE,
    DiscrepancyType,
    OrderDiscrepancy,
    ReconciledReason,
    StatusContradictionReason,
    compute_money_at_risk,
    reconcile,
)
from app.ingest.parsing import OrderRow, PaymentRow, parse_orders_csv, parse_payments_csv

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ORDERS_CSV = FIXTURES_DIR / "mini_orders.csv"
PAYMENTS_CSV = FIXTURES_DIR / "mini_payments.csv"


@pytest.fixture(scope="module")
def result():
    orders = parse_orders_csv(ORDERS_CSV)
    payments = parse_payments_csv(PAYMENTS_CSV)
    return reconcile(orders, payments)


def _type_by_id(result, order_id_norm: str) -> DiscrepancyType:
    for d in result.order_discrepancies:
        if d.order.order_id_norm == order_id_norm:
            return d.type
    raise AssertionError(f"no discrepancy found for {order_id_norm}")


def _discrepancy_by_id(result, order_id_norm: str) -> OrderDiscrepancy:
    for d in result.order_discrepancies:
        if d.order.order_id_norm == order_id_norm:
            return d
    raise AssertionError(f"no discrepancy found for {order_id_norm}")


# -- per-order expected classification (the brief's exact table) -------


@pytest.mark.parametrize(
    "order_id,expected_type",
    [
        ("FIX-1001", DiscrepancyType.RECONCILED),
        ("FIX-1002", DiscrepancyType.MISSING_PAYMENT),
        ("FIX-1003", DiscrepancyType.DUPLICATE_CHARGE),
        ("FIX-1004", DiscrepancyType.CURRENCY_MISMATCH),
        ("FIX-1005", DiscrepancyType.AMOUNT_MISMATCH),
        ("FIX-1006", DiscrepancyType.RECONCILED),
        ("FIX-1007", DiscrepancyType.UNSETTLED_PAYMENT),
        ("FIX-1008", DiscrepancyType.UNSETTLED_PAYMENT),
        ("FIX-1009", DiscrepancyType.STATUS_CONTRADICTION),
        ("FIX-1010", DiscrepancyType.STATUS_CONTRADICTION),
        ("FIX-1011", DiscrepancyType.RECONCILED),
        ("FIX-1012", DiscrepancyType.RECONCILED),
        ("FIX-1013", DiscrepancyType.RECONCILED),
        ("FIX-1014", DiscrepancyType.RECONCILED),
    ],
)
def test_order_classification(result, order_id, expected_type):
    assert _type_by_id(result, order_id) == expected_type


def test_fix_1009_is_cancelled_but_charged_flavor(result):
    d = _discrepancy_by_id(result, "FIX-1009")
    assert d.status_contradiction_reason == StatusContradictionReason.CANCELLED_BUT_CHARGED


def test_fix_1010_is_completed_but_refunded_flavor(result):
    d = _discrepancy_by_id(result, "FIX-1010")
    assert d.status_contradiction_reason == StatusContradictionReason.COMPLETED_BUT_REFUNDED


def test_fix_1012_matches_after_ref_normalization(result):
    # payment row has order_reference " fix-1012 " (mangled case/whitespace)
    d = _discrepancy_by_id(result, "FIX-1012")
    assert len(d.matched_payments) == 1
    assert d.matched_payments[0].order_reference_norm == "FIX-1012"


def test_orphan_payment_for_txn_fix_099(result):
    assert len(result.orphan_payments) == 1
    assert result.orphan_payments[0].transaction_ref == "TXN-FIX-099"


# -- aggregate expectations ---------------------------------------------


def test_total_unique_orders(result):
    assert result.total_orders == 14


def test_reconciled_count_and_value(result):
    assert result.counts[DiscrepancyType.RECONCILED] == 6
    assert result.reconciled_value == Decimal("570.00")


def test_singleton_discrepancy_counts(result):
    assert result.counts[DiscrepancyType.MISSING_PAYMENT] == 1
    assert result.counts[DiscrepancyType.DUPLICATE_CHARGE] == 1
    assert result.counts[DiscrepancyType.CURRENCY_MISMATCH] == 1
    assert result.counts[DiscrepancyType.AMOUNT_MISMATCH] == 1
    assert result.counts[DiscrepancyType.ORPHAN_PAYMENT] == 1


def test_doubled_discrepancy_counts(result):
    assert result.counts[DiscrepancyType.UNSETTLED_PAYMENT] == 2
    assert result.counts[DiscrepancyType.STATUS_CONTRADICTION] == 2


def test_money_at_risk_matches_expected_total(result):
    # $75.00 (FIX-1002 missing) + $60.00 + $80.00 (FIX-1007/1008 unsettled)
    # + $24.50 (FIX-1005 overcharge) + $120.00 (FIX-1009 cancelled-but-charged)
    # = $359.50 -- FIX-1010 (completed-but-refunded) contributes $0.
    assert result.money_at_risk == Decimal("359.50")


def test_amount_mismatch_diff_is_signed_overcharge(result):
    d = _discrepancy_by_id(result, "FIX-1005")
    assert d.amount_diff == Decimal("24.50")


def test_tolerance_is_fixed_two_cents(result):
    assert AMOUNT_TOLERANCE == Decimal("0.02")
    # FIX-1006: order $50.00 vs payment $50.01, diff $0.01 -- inside tolerance.
    d = _discrepancy_by_id(result, "FIX-1006")
    assert d.type == DiscrepancyType.RECONCILED
    assert d.reconciled_reason == ReconciledReason.VERIFIED


# -- compute_money_at_risk unit-tested directly, with hand-built rows ---


def _order(order_id: str, net_amount: str, status: str = "completed", currency: str = "USD") -> OrderRow:
    return OrderRow(
        order_id=order_id,
        order_id_norm=order_id,
        order_date=None,
        customer_email=None,
        currency=currency,
        gross_amount=Decimal(net_amount),
        discount=Decimal("0"),
        net_amount=Decimal(net_amount),
        status=status,
    )


def _payment(
    order_reference: str,
    amount: str,
    currency: str = "USD",
    type: str = "charge",
    status: str = "settled",
    transaction_ref: str = "TXN-X",
) -> PaymentRow:
    return PaymentRow(
        transaction_ref=transaction_ref,
        processed_at=None,
        order_reference=order_reference,
        order_reference_norm=order_reference,
        currency=currency,
        amount=Decimal(amount),
        fee=Decimal("0"),
        net_settled=None,
        type=type,
        status=status,
    )


# -- RECONCILED sub-flavors: verified match vs. benign no-activity fallback --


def test_fallback_no_charge_activity_is_reconciled_but_not_counted_as_value():
    # A non-completed, non-cancelled order (e.g. still pending) with zero
    # matched payments at all: not a discrepancy, but also not a *verified*
    # match -- nothing was actually checked against a settled payment, so
    # it must not silently inflate reconciled_value.
    order = _order("PEND-1", "500.00", status="pending")
    result = reconcile([order], [])
    d = result.order_discrepancies[0]
    assert d.type == DiscrepancyType.RECONCILED
    assert d.reconciled_reason == ReconciledReason.NO_CHARGE_ACTIVITY
    assert result.reconciled_value == Decimal("0")


def test_fallback_unverified_pending_charge_not_counted_as_value():
    # A non-completed order with only a pending (never-settled) charge
    # attempt -- UNSETTLED_PAYMENT is gated on order.status == completed,
    # so this also falls through to the RECONCILED/NO_CHARGE_ACTIVITY
    # fallback and must not count toward reconciled_value either.
    order = _order("PEND-2", "250.00", status="pending")
    payment = _payment("PEND-2", "250.00", status="pending")
    result = reconcile([order], [payment])
    d = result.order_discrepancies[0]
    assert d.type == DiscrepancyType.RECONCILED
    assert d.reconciled_reason == ReconciledReason.NO_CHARGE_ACTIVITY
    assert result.reconciled_value == Decimal("0")


def test_verified_reconciled_order_is_counted_as_value():
    order = _order("OK-1", "100.00")
    payment = _payment("OK-1", "100.00")
    result = reconcile([order], [payment])
    d = result.order_discrepancies[0]
    assert d.type == DiscrepancyType.RECONCILED
    assert d.reconciled_reason == ReconciledReason.VERIFIED
    assert result.reconciled_value == Decimal("100.00")


# -- exact tolerance boundary ($0.02) -----------------------------------


def test_diff_exactly_at_tolerance_stays_reconciled_and_counted():
    order = _order("TOL-1", "100.00")
    payment = _payment("TOL-1", "100.02")  # diff == 0.02, boundary inclusive
    result = reconcile([order], [payment])
    d = result.order_discrepancies[0]
    assert d.type == DiscrepancyType.RECONCILED
    assert d.reconciled_reason == ReconciledReason.VERIFIED
    assert result.reconciled_value == Decimal("100.00")


def test_diff_just_beyond_tolerance_flips_to_amount_mismatch():
    order = _order("TOL-2", "100.00")
    payment = _payment("TOL-2", "100.021")  # diff == 0.021, just past 0.02
    result = reconcile([order], [payment])
    assert result.order_discrepancies[0].type == DiscrepancyType.AMOUNT_MISMATCH


def test_diff_of_three_cents_is_amount_mismatch():
    order = _order("TOL-3", "100.00")
    payment = _payment("TOL-3", "100.03")  # diff == 0.03
    result = reconcile([order], [payment])
    assert result.order_discrepancies[0].type == DiscrepancyType.AMOUNT_MISMATCH


def test_compute_money_at_risk_sums_all_three_components():
    missing = OrderDiscrepancy(
        order=_order("A", "10.00"),
        type=DiscrepancyType.MISSING_PAYMENT,
        matched_payments=(),
        reason="",
    )
    unsettled = OrderDiscrepancy(
        order=_order("B", "20.00"),
        type=DiscrepancyType.UNSETTLED_PAYMENT,
        matched_payments=(),
        reason="",
    )
    overcharge = OrderDiscrepancy(
        order=_order("C", "30.00"),
        type=DiscrepancyType.AMOUNT_MISMATCH,
        matched_payments=(),
        reason="",
        amount_diff=Decimal("5.00"),
    )
    undercharge = OrderDiscrepancy(
        order=_order("D", "30.00"),
        type=DiscrepancyType.AMOUNT_MISMATCH,
        matched_payments=(),
        reason="",
        amount_diff=Decimal("-3.00"),
    )
    cancelled_charged = OrderDiscrepancy(
        order=_order("E", "40.00", status="cancelled"),
        type=DiscrepancyType.STATUS_CONTRADICTION,
        matched_payments=(),
        reason="",
        status_contradiction_reason=StatusContradictionReason.CANCELLED_BUT_CHARGED,
    )
    completed_refunded = OrderDiscrepancy(
        order=_order("F", "999.00"),
        type=DiscrepancyType.STATUS_CONTRADICTION,
        matched_payments=(),
        reason="",
        status_contradiction_reason=StatusContradictionReason.COMPLETED_BUT_REFUNDED,
    )
    reconciled = OrderDiscrepancy(
        order=_order("G", "50.00"),
        type=DiscrepancyType.RECONCILED,
        matched_payments=(),
        reason="",
    )

    total = compute_money_at_risk(
        [missing, unsettled, overcharge, undercharge, cancelled_charged, completed_refunded, reconciled]
    )

    # 10 + 20 + 5 - 3 + 40 + 0 (completed_refunded) + 0 (reconciled) = 72
    assert total == Decimal("72.00")


def test_compute_money_at_risk_completed_but_refunded_contributes_zero():
    completed_refunded = OrderDiscrepancy(
        order=_order("F", "999.00"),
        type=DiscrepancyType.STATUS_CONTRADICTION,
        matched_payments=(),
        reason="",
        status_contradiction_reason=StatusContradictionReason.COMPLETED_BUT_REFUNDED,
    )
    assert compute_money_at_risk([completed_refunded]) == Decimal("0")


def test_compute_money_at_risk_empty_is_zero():
    assert compute_money_at_risk([]) == Decimal("0")
