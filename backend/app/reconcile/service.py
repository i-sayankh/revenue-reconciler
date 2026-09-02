"""DB queries + persistence shaping for the reconciliation run/discrepancy
routes (`backend/app/routers/reconcile.py`).

This module is the seam between the deterministic engine
(`app.engine.reconcile.reconcile`, which only ever sees plain
`OrderRow`/`PaymentRow` objects and knows nothing about the database) and
Postgres. It is responsible for:

- fetching the caller's *latest uploaded batch* of orders and of payments
  (independently -- see module docstring note below) and converting the DB
  row models (`app.models.Order`/`Payment`) into the engine's input types,
  since the engine module must never import a DB driver;
- shaping the engine's `ReconciliationResult` into rows for the
  `reconciliation_runs` and `discrepancies` tables and persisting them;
- reading the persisted run/discrepancy rows back for the two GET routes.

All functions here take an already-connected `asyncpg.Connection` (or, in
tests, a fake object exposing the same `fetch`/`fetchrow`/`executemany`
surface -- see `backend/tests/test_reconcile.py`), the same pattern as
`app.ingest.loader`.

Latest batch semantics
-----------------------
Orders and payments are uploaded via two separate calls (Step 5b), each
minting its own `upload_batch_id`, so they generally do *not* share a batch
id or a timestamp. "The caller's latest uploaded orders+payments" means:
the most recent `upload_batch_id` present in `orders` for that user, and
*separately* the most recent `upload_batch_id` present in `payments` for
that user -- not a joined/paired batch.

total_reconciled_value vs. total_disputed_value vs. money_at_risk
--------------------------------------------------------------------
- `total_reconciled_value` is the engine's own `ReconciliationResult.
  reconciled_value` -- the sum of `net_amount` across VERIFIED-RECONCILED
  orders only (deliberately excluding the NO_CHARGE_ACTIVITY fallback
  flavor of RECONCILED; see the engine's docstring for why).
- `total_disputed_value` is a wider net than `money_at_risk`: it is the sum
  of the "relevant value" (an order's `net_amount` for order-side
  discrepancies, a payment's `amount` for orphan payments -- there is no
  order to attach a value to) across *every* discrepancy that is not
  RECONCILED. This intentionally includes cases that are not money at risk
  (e.g. a CURRENCY_MISMATCH, or the COMPLETED_BUT_REFUNDED flavor of
  STATUS_CONTRADICTION, where the money has already moved) because they
  still need a human to look at them.
- `money_at_risk` is exactly the engine's own `ReconciliationResult.
  money_at_risk` (see `compute_money_at_risk`'s docstring for its formula).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import asyncpg

from app.engine.reconcile import (
    DiscrepancyType,
    OrderDiscrepancy,
    ReconciliationResult,
    reconcile,
)
from app.ingest.parsing import OrderRow, PaymentRow
from app.models import Discrepancy, Order, Payment, ReconciliationRun

_CHARGE = "charge"
_SETTLED = "settled"


# -- fetch latest uploaded batches ---------------------------------------


async def fetch_latest_orders(connection: asyncpg.Connection, user_id: str | uuid.UUID) -> list[Order]:
    """All order rows from the user's most recent order upload batch.

    Empty list if the user has never uploaded any orders.
    """
    user_uuid = uuid.UUID(str(user_id))
    records = await connection.fetch(
        """
        select * from orders
        where user_id = $1
          and upload_batch_id = (
              select upload_batch_id from orders
              where user_id = $1
              order by created_at desc
              limit 1
          )
        """,
        user_uuid,
    )
    return [Order.from_record(r) for r in records]


async def fetch_latest_payments(connection: asyncpg.Connection, user_id: str | uuid.UUID) -> list[Payment]:
    """All payment rows from the user's most recent payment upload batch.

    Empty list if the user has never uploaded any payments.
    """
    user_uuid = uuid.UUID(str(user_id))
    records = await connection.fetch(
        """
        select * from payments
        where user_id = $1
          and upload_batch_id = (
              select upload_batch_id from payments
              where user_id = $1
              order by created_at desc
              limit 1
          )
        """,
        user_uuid,
    )
    return [Payment.from_record(r) for r in records]


# -- DB model -> engine input row conversion -----------------------------


def _order_to_row(order: Order) -> OrderRow:
    return OrderRow(
        order_id=order.order_id,
        order_id_norm=order.order_id_norm,
        order_date=order.order_date,
        customer_email=order.customer_email,
        currency=order.currency,
        gross_amount=order.gross_amount,
        discount=order.discount,
        net_amount=order.net_amount,
        status=order.status,
    )


def _payment_to_row(payment: Payment) -> PaymentRow:
    return PaymentRow(
        transaction_ref=payment.transaction_ref,
        processed_at=payment.processed_at,
        order_reference=payment.order_reference,
        order_reference_norm=payment.order_reference_norm,
        currency=payment.currency,
        amount=payment.amount,
        fee=payment.fee,
        net_settled=payment.net_settled,
        type=payment.type,
        status=payment.status,
    )


def run_reconciliation(orders: list[Order], payments: list[Payment]) -> ReconciliationResult:
    """Convert DB rows to engine input and run the deterministic engine."""
    order_rows = [_order_to_row(o) for o in orders]
    payment_rows = [_payment_to_row(p) for p in payments]
    return reconcile(order_rows, payment_rows)


# -- shaping the engine result into discrepancy rows ---------------------


def _primary_payment(d: OrderDiscrepancy) -> PaymentRow | None:
    """The single payment most representative of this discrepancy, if any.

    Preference order: a settled charge (the normal case -- this is what
    the engine itself compares against), else any charge (covers
    UNSETTLED_PAYMENT, where the matched charge never settled), else
    whatever else was matched (e.g. a lone refund with no charge, an
    edge case the engine tolerates but does not name). `None` when
    nothing at all was matched (e.g. MISSING_PAYMENT).
    """
    charges = [p for p in d.matched_payments if p.type == _CHARGE]
    settled_charges = [p for p in charges if p.status == _SETTLED]
    if settled_charges:
        return settled_charges[0]
    if charges:
        return charges[0]
    if d.matched_payments:
        return d.matched_payments[0]
    return None


def _discrepancy_detail(d: OrderDiscrepancy) -> dict[str, Any]:
    detail: dict[str, Any] = {"reason": d.reason}
    if d.status_contradiction_reason is not None:
        detail["status_contradiction_reason"] = d.status_contradiction_reason.value
    if d.reconciled_reason is not None:
        detail["reconciled_reason"] = d.reconciled_reason.value
    if len(d.matched_payments) > 1:
        detail["matched_payment_refs"] = [p.transaction_ref for p in d.matched_payments]
    return detail


@dataclass(frozen=True, slots=True)
class DiscrepancyInsert:
    """One row's worth of values for the `discrepancies` insert."""

    type: str
    order_id: str | None
    payment_ref: str | None
    order_amount: Decimal | None
    payment_amount: Decimal | None
    currency_order: str | None
    currency_payment: str | None
    difference: Decimal | None
    detail: dict[str, Any]


def build_discrepancy_inserts(result: ReconciliationResult) -> list[DiscrepancyInsert]:
    """Shape every per-order discrepancy plus every orphan payment into rows.

    Order-side rows (including RECONCILED ones -- every order gets a row,
    since `discrepancies` is really "per-order classification result", not
    only the exceptions) come from `result.order_discrepancies`.
    `ORPHAN_PAYMENT` rows (no matching order at all) come from
    `result.orphan_payments` and have no `order_id`/`order_amount`.
    """
    inserts: list[DiscrepancyInsert] = []
    for d in result.order_discrepancies:
        primary = _primary_payment(d)
        inserts.append(
            DiscrepancyInsert(
                type=d.type.value,
                order_id=d.order.order_id,
                payment_ref=primary.transaction_ref if primary else None,
                order_amount=d.order.net_amount,
                payment_amount=primary.amount if primary else None,
                currency_order=d.order.currency,
                currency_payment=primary.currency if primary else None,
                difference=d.amount_diff,
                detail=_discrepancy_detail(d),
            )
        )
    for p in result.orphan_payments:
        inserts.append(
            DiscrepancyInsert(
                type=DiscrepancyType.ORPHAN_PAYMENT.value,
                order_id=None,
                payment_ref=p.transaction_ref,
                order_amount=None,
                payment_amount=p.amount,
                currency_order=None,
                currency_payment=p.currency,
                difference=None,
                detail={
                    "reason": "settled charge payment matched no known order",
                    "order_reference": p.order_reference,
                },
            )
        )
    return inserts


def compute_disputed_value(result: ReconciliationResult) -> Decimal:
    """Sum of the "relevant value" across every non-RECONCILED discrepancy.

    See the module docstring for how this differs from both
    `result.reconciled_value` and `result.money_at_risk`.
    """
    total = Decimal("0")
    for d in result.order_discrepancies:
        if d.type != DiscrepancyType.RECONCILED:
            total += d.order.net_amount
    for p in result.orphan_payments:
        total += p.amount
    return total


# -- persistence ------------------------------------------------------------

_INSERT_RUN_SQL = """
    insert into reconciliation_runs (
        user_id, orders_count, payments_count, total_reconciled_value,
        total_disputed_value, money_at_risk, status
    ) values ($1, $2, $3, $4, $5, $6, $7)
    returning *
"""

_INSERT_DISCREPANCIES_SQL = """
    insert into discrepancies (
        run_id, user_id, type, order_id, payment_ref, order_amount,
        payment_amount, currency_order, currency_payment, difference, detail
    ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""


async def persist_run(
    connection: asyncpg.Connection,
    *,
    user_id: str | uuid.UUID,
    result: ReconciliationResult,
    orders_count: int,
    payments_count: int,
) -> ReconciliationRun:
    """Insert one `reconciliation_runs` row and one `discrepancies` row per
    per-order classification + orphan payment, and return the persisted run.
    """
    user_uuid = uuid.UUID(str(user_id))
    record = await connection.fetchrow(
        _INSERT_RUN_SQL,
        user_uuid,
        orders_count,
        payments_count,
        result.reconciled_value,
        compute_disputed_value(result),
        result.money_at_risk,
        "complete",
    )
    run = ReconciliationRun.from_record(record)

    inserts = build_discrepancy_inserts(result)
    if inserts:
        values = [
            (
                run.id,
                user_uuid,
                item.type,
                item.order_id,
                item.payment_ref,
                item.order_amount,
                item.payment_amount,
                item.currency_order,
                item.currency_payment,
                item.difference,
                item.detail,
            )
            for item in inserts
        ]
        await connection.executemany(_INSERT_DISCREPANCIES_SQL, values)

    return run


# -- read-back for GET /api/reconcile/runs/latest and GET /api/discrepancies --


async def fetch_latest_run(connection: asyncpg.Connection, user_id: str | uuid.UUID) -> ReconciliationRun | None:
    """The user's most recent reconciliation run, or `None` if they have
    never run one."""
    user_uuid = uuid.UUID(str(user_id))
    record = await connection.fetchrow(
        "select * from reconciliation_runs where user_id = $1 order by created_at desc limit 1",
        user_uuid,
    )
    return ReconciliationRun.from_record(record) if record else None


async def fetch_by_type_summary(connection: asyncpg.Connection, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Count and total value per discrepancy type for one run.

    "Value" is `order_amount` when present, falling back to `payment_amount`
    for ORPHAN_PAYMENT rows (which have no order to attach a value to).
    Only types with at least one row appear (no zero-count rows); ordered
    alphabetically by type for a stable response.
    """
    records = await connection.fetch(
        """
        select type,
               count(*) as count,
               coalesce(sum(coalesce(order_amount, payment_amount)), 0) as value
        from discrepancies
        where run_id = $1
        group by type
        order by type
        """,
        run_id,
    )
    return [{"type": r["type"], "count": r["count"], "value": r["value"]} for r in records]


def _discrepancy_filter_clauses(
    *,
    run_id: uuid.UUID,
    type_: str | None,
    q: str | None,
    min_amount: Decimal | None,
    max_amount: Decimal | None,
) -> tuple[str, list[Any]]:
    conditions = ["run_id = $1"]
    params: list[Any] = [run_id]

    if type_:
        params.append(type_)
        conditions.append(f"type = ${len(params)}")
    if q:
        params.append(f"%{q}%")
        idx = len(params)
        conditions.append(f"(order_id ilike ${idx} or payment_ref ilike ${idx})")
    if min_amount is not None:
        params.append(min_amount)
        conditions.append(f"order_amount >= ${len(params)}")
    if max_amount is not None:
        params.append(max_amount)
        conditions.append(f"order_amount <= ${len(params)}")

    return " and ".join(conditions), params


async def fetch_discrepancies(
    connection: asyncpg.Connection,
    *,
    run_id: uuid.UUID,
    type_: str | None = None,
    q: str | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Discrepancy], int]:
    """Paginated, filtered discrepancy rows for one run, plus the total
    matching count (ignoring pagination) for building pagination controls.

    Filters (all optional, AND-combined): `type_` (exact match), `q`
    (case-insensitive substring over `order_id` OR `payment_ref`),
    `min_amount`/`max_amount` (inclusive range over `order_amount` -- rows
    with a null `order_amount`, i.e. ORPHAN_PAYMENT, are excluded whenever
    either bound is set, since SQL comparisons against null are never true).
    """
    where_clause, params = _discrepancy_filter_clauses(
        run_id=run_id, type_=type_, q=q, min_amount=min_amount, max_amount=max_amount
    )

    total_record = await connection.fetchrow(
        f"select count(*) as total from discrepancies where {where_clause}", *params
    )
    total = total_record["total"]

    limit_idx = len(params) + 1
    offset_idx = len(params) + 2
    offset = (page - 1) * page_size
    records = await connection.fetch(
        f"""
        select * from discrepancies
        where {where_clause}
        order by order_amount desc nulls last, id
        limit ${limit_idx} offset ${offset_idx}
        """,
        *params,
        page_size,
        offset,
    )
    rows = [Discrepancy.from_record(r) for r in records]
    return rows, total
