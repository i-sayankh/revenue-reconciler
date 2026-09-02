"""Deterministic reconciliation engine.

HARD CONSTRAINT: this module must never import a DB driver, an HTTP client,
or an LLM SDK (no `httpx`, `requests`, Groq SDK, `sqlalchemy`, etc). It takes
already-parsed `OrderRow`/`PaymentRow` objects (see `app.ingest.parsing`) in
memory and returns plain-Python classification results. It must be fully
unit-testable offline. The LLM layer (a later step) only *explains* the
results this module produces -- it never decides a match or a discrepancy
type.

Match key
---------
Orders and payments are matched on `order.order_id_norm ==
payment.order_reference_norm`. Both are already normalized (`strip().upper()`)
by the Step 5a parser -- this module does not re-normalize them.

Discrepancy taxonomy
---------------------
See `DiscrepancyType` below. Tolerance for amount comparison is a fixed
**$0.02 absolute** (not a percentage), compared against the order's
`net_amount` (never `gross_amount`) and the payment's `amount` (never
`net_settled` -- `net_settled = amount - fee` and the processor fee is a
cost of doing business, not a discrepancy between what the store expected
and what the processor recorded).

Evaluation priority
--------------------
An order can plausibly satisfy more than one condition at once (e.g. a
duplicate-charged order might also look like it has an "amount mismatch"
if you naively compare the order total against just one of the two charges).
Conditions are therefore evaluated in this order, first match wins:

1. `MISSING_PAYMENT` -- order is `completed` and there is no charge-type
   payment matched at all. Nothing else to check once there's no payment.
2. `DUPLICATE_CHARGE` -- more than one *settled* charge matched and no
   settled refund offsets any of them. Checked early because if the order
   was double-billed, that fact is more important/actionable than whatever
   a currency/amount comparison against one of the two charges would say.
3. `STATUS_CONTRADICTION` -- the order's status flatly disagrees with the
   charge/refund trail (cancelled-but-still-charged, or
   completed-but-actually-refunded). Checked before currency/amount/
   settlement checks because those checks assume the order status is a
   trustworthy frame to interpret the payment against; if the order's own
   status is wrong, that's the more fundamental problem to surface.
4. `UNSETTLED_PAYMENT` -- order `completed`, but the (only) matched charge
   never settled (`failed`/`pending`). Checked before currency/amount
   because comparing currency or amount on a payment that never actually
   went through is not meaningful -- there's no settled money to compare.
5. `CURRENCY_MISMATCH` -- checked before amount, since comparing raw
   amounts across two different currencies is meaningless (no FX
   conversion is in scope here).
6. `AMOUNT_MISMATCH` -- same currency, but the settled charge amount and
   the order's net amount differ by more than the $0.02 tolerance.
7. `RECONCILED` -- everything else: matched, same currency, within
   tolerance, payment settled, order status agrees with the charge/refund
   trail.

`ORPHAN_PAYMENT` is not part of the per-order priority chain above -- it is
evaluated from the *payment* side: any settled charge payment whose
`order_reference_norm` matches no known order at all.

STATUS_CONTRADICTION has two internal sub-flavors
--------------------------------------------------
Both surface as the same top-level `DiscrepancyType.STATUS_CONTRADICTION`,
but they are financially very different, so `OrderDiscrepancy` carries a
`status_contradiction_reason` (see `StatusContradictionReason`) to tell them
apart internally:

- `CANCELLED_BUT_CHARGED`: the order says `cancelled` but a settled charge
  exists that was never offset by a settled refund. The money is still out
  the door -- this is real money at risk until it's refunded.
- `COMPLETED_BUT_REFUNDED`: the order still says `completed`, but the
  charge/refund trail shows the money was already given back. No money is
  actually at risk here -- the books already reflect the refund; the only
  problem is that nobody updated the order's status to `refunded`/
  `cancelled`. It's a data-hygiene issue, not a financial one.

See `compute_money_at_risk` for how this distinction is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from app.ingest.parsing import OrderRow, PaymentRow

AMOUNT_TOLERANCE = Decimal("0.02")

_CHARGE = "charge"
_REFUND = "refund"
_SETTLED = "settled"
_COMPLETED = "completed"
_CANCELLED = "cancelled"


class DiscrepancyType(str, Enum):
    MISSING_PAYMENT = "MISSING_PAYMENT"
    ORPHAN_PAYMENT = "ORPHAN_PAYMENT"
    DUPLICATE_CHARGE = "DUPLICATE_CHARGE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    UNSETTLED_PAYMENT = "UNSETTLED_PAYMENT"
    STATUS_CONTRADICTION = "STATUS_CONTRADICTION"
    RECONCILED = "RECONCILED"


class StatusContradictionReason(str, Enum):
    """Internal sub-flavor of STATUS_CONTRADICTION -- see module docstring."""

    CANCELLED_BUT_CHARGED = "cancelled_but_charged"
    COMPLETED_BUT_REFUNDED = "completed_but_refunded"


@dataclass(frozen=True, slots=True)
class OrderDiscrepancy:
    """The classification result for a single order."""

    order: OrderRow
    type: DiscrepancyType
    matched_payments: tuple[PaymentRow, ...]
    reason: str
    status_contradiction_reason: StatusContradictionReason | None = None
    amount_diff: Decimal | None = None  # payment.amount - order.net_amount


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Everything Step 7 (and the report/API layer) needs from one run."""

    order_discrepancies: tuple[OrderDiscrepancy, ...]
    orphan_payments: tuple[PaymentRow, ...]

    @property
    def counts(self) -> dict[DiscrepancyType, int]:
        counts: dict[DiscrepancyType, int] = {t: 0 for t in DiscrepancyType}
        for d in self.order_discrepancies:
            counts[d.type] += 1
        counts[DiscrepancyType.ORPHAN_PAYMENT] = len(self.orphan_payments)
        return counts

    @property
    def total_orders(self) -> int:
        return len(self.order_discrepancies)

    @property
    def reconciled_value(self) -> Decimal:
        """Sum of `order.net_amount` for every RECONCILED order."""
        return sum(
            (d.order.net_amount for d in self.order_discrepancies if d.type == DiscrepancyType.RECONCILED),
            Decimal("0"),
        )

    @property
    def money_at_risk(self) -> Decimal:
        return compute_money_at_risk(self.order_discrepancies)


def _dedup_orders(orders: list[OrderRow]) -> list[OrderRow]:
    """Collapse orders sharing the same normalized id, keeping the first.

    Step 5a's parser already drops byte-for-byte duplicate CSV rows, but
    the engine is defensive here too (e.g. two rows for the same order id
    with different content should still count as one order, not two) so
    downstream consumers never have to worry about it.
    """
    seen: set[str] = set()
    deduped: list[OrderRow] = []
    for order in orders:
        if order.order_id_norm in seen:
            continue
        seen.add(order.order_id_norm)
        deduped.append(order)
    return deduped


def _classify_order(order: OrderRow, matched: list[PaymentRow]) -> OrderDiscrepancy:
    charges = [p for p in matched if p.type == _CHARGE]
    settled_charges = [p for p in charges if p.status == _SETTLED]
    settled_refunds = [p for p in matched if p.type == _REFUND and p.status == _SETTLED]
    unsettled_charges = [p for p in charges if p.status != _SETTLED]

    # 1. MISSING_PAYMENT -- completed order, no charge-type payment at all.
    if order.status == _COMPLETED and not charges:
        return OrderDiscrepancy(
            order=order,
            type=DiscrepancyType.MISSING_PAYMENT,
            matched_payments=tuple(matched),
            reason="order is completed but no charge payment was matched",
        )

    # 2. DUPLICATE_CHARGE -- more than one settled charge, no offsetting refund.
    if len(settled_charges) > 1 and not settled_refunds:
        return OrderDiscrepancy(
            order=order,
            type=DiscrepancyType.DUPLICATE_CHARGE,
            matched_payments=tuple(matched),
            reason=f"{len(settled_charges)} settled charges matched, no refund offsetting them",
        )

    # 3. STATUS_CONTRADICTION -- order status disagrees with the charge/refund trail.
    if order.status == _CANCELLED and len(settled_charges) > len(settled_refunds):
        return OrderDiscrepancy(
            order=order,
            type=DiscrepancyType.STATUS_CONTRADICTION,
            matched_payments=tuple(matched),
            reason="order is cancelled but a settled charge was never refunded",
            status_contradiction_reason=StatusContradictionReason.CANCELLED_BUT_CHARGED,
        )
    if order.status == _COMPLETED and settled_refunds:
        return OrderDiscrepancy(
            order=order,
            type=DiscrepancyType.STATUS_CONTRADICTION,
            matched_payments=tuple(matched),
            reason="order is still marked completed but the charge was refunded",
            status_contradiction_reason=StatusContradictionReason.COMPLETED_BUT_REFUNDED,
        )

    # From here on, the "primary" payment representing this order's charge
    # is the settled charge (there is at most one left, by construction --
    # duplicates and unrefunded-cancelled-charges were handled above).
    primary_charge = settled_charges[0] if settled_charges else None

    # 4. UNSETTLED_PAYMENT -- completed order, matched charge never settled.
    if order.status == _COMPLETED and primary_charge is None and unsettled_charges:
        bad = unsettled_charges[0]
        return OrderDiscrepancy(
            order=order,
            type=DiscrepancyType.UNSETTLED_PAYMENT,
            matched_payments=tuple(matched),
            reason=f"matched charge payment status is '{bad.status}', not settled",
        )

    # Nothing further to compare against if there is no settled charge
    # (e.g. a non-completed order with only a pending/failed charge, or no
    # charge at all) -- there's no discrepancy to raise in that case.
    if primary_charge is None:
        return OrderDiscrepancy(
            order=order,
            type=DiscrepancyType.RECONCILED,
            matched_payments=tuple(matched),
            reason="no settled charge to reconcile against; nothing outstanding",
        )

    # 5. CURRENCY_MISMATCH -- comparing amounts across currencies is meaningless.
    if order.currency != primary_charge.currency:
        return OrderDiscrepancy(
            order=order,
            type=DiscrepancyType.CURRENCY_MISMATCH,
            matched_payments=tuple(matched),
            reason=f"order currency {order.currency} != payment currency {primary_charge.currency}",
        )

    # 6. AMOUNT_MISMATCH -- same currency, diff beyond the fixed $0.02 tolerance.
    diff = primary_charge.amount - order.net_amount
    if abs(diff) > AMOUNT_TOLERANCE:
        return OrderDiscrepancy(
            order=order,
            type=DiscrepancyType.AMOUNT_MISMATCH,
            matched_payments=tuple(matched),
            reason=f"order net_amount {order.net_amount} vs payment amount {primary_charge.amount} (diff {diff})",
            amount_diff=diff,
        )

    # 7. RECONCILED -- matched, same currency, within tolerance, settled, agrees.
    return OrderDiscrepancy(
        order=order,
        type=DiscrepancyType.RECONCILED,
        matched_payments=tuple(matched),
        reason="matched, same currency, within tolerance, settled",
    )


def reconcile(orders: list[OrderRow], payments: list[PaymentRow]) -> ReconciliationResult:
    """Classify every order and find orphan payments.

    Pure function: no I/O, no DB, no network. `orders`/`payments` are the
    already-parsed, already-normalized rows from `app.ingest.parsing`.
    """
    deduped_orders = _dedup_orders(orders)
    order_ids_norm = {o.order_id_norm for o in deduped_orders}

    payments_by_order: dict[str, list[PaymentRow]] = {}
    for payment in payments:
        payments_by_order.setdefault(payment.order_reference_norm, []).append(payment)

    order_discrepancies = tuple(
        _classify_order(order, payments_by_order.get(order.order_id_norm, [])) for order in deduped_orders
    )

    orphan_payments = tuple(
        p
        for p in payments
        if p.type == _CHARGE and p.status == _SETTLED and p.order_reference_norm not in order_ids_norm
    )

    return ReconciliationResult(order_discrepancies=order_discrepancies, orphan_payments=orphan_payments)


def compute_money_at_risk(order_discrepancies: "tuple[OrderDiscrepancy, ...] | list[OrderDiscrepancy]") -> Decimal:
    """Sum of money genuinely at risk right now, per the plan's formula.

    1. Full `order.net_amount` for every MISSING_PAYMENT and
       UNSETTLED_PAYMENT discrepancy -- the store is owed this money and
       has not (yet, verifiably) received it.
    2. The *signed* net overcharge/undercharge for every AMOUNT_MISMATCH
       (`payment.amount - order.net_amount`) -- an overcharge adds to risk
       (money that may need to be refunded), an undercharge subtracts from
       it (money the store is still short).
    3. The full order value for the CANCELLED_BUT_CHARGED flavor of
       STATUS_CONTRADICTION only -- the order was cancelled but the charge
       was never refunded, so that money is sitting with the store despite
       the order no longer being valid.

    The COMPLETED_BUT_REFUNDED flavor of STATUS_CONTRADICTION deliberately
    contributes $0: the refund already happened, so the money has already
    moved and the books already reflect it. That case is a status/data-
    hygiene problem (an order stuck showing `completed` when it should say
    `refunded`/`cancelled`), not money currently at risk -- including it
    here would double-count money that isn't actually exposed.
    """
    total = Decimal("0")
    for d in order_discrepancies:
        if d.type in (DiscrepancyType.MISSING_PAYMENT, DiscrepancyType.UNSETTLED_PAYMENT):
            total += d.order.net_amount
        elif d.type == DiscrepancyType.AMOUNT_MISMATCH:
            total += d.amount_diff if d.amount_diff is not None else Decimal("0")
        elif (
            d.type == DiscrepancyType.STATUS_CONTRADICTION
            and d.status_contradiction_reason == StatusContradictionReason.CANCELLED_BUT_CHARGED
        ):
            total += d.order.net_amount
        # COMPLETED_BUT_REFUNDED and RECONCILED contribute $0 -- see docstring.
    return total
