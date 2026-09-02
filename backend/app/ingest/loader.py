"""Per-row validation and DB inserts for the ingest endpoints.

`app.ingest.parsing.parse_orders_csv`/`parse_payments_csv` (Step 5a) parse a
whole file in one pass and raise on the first malformed row -- correct for a
pure parsing library, but the ingest endpoints need per-row fault tolerance:
one bad row should be reported as a rejection, not abort the whole upload.

This module re-walks the same CSV rows one at a time, reusing Step 5a's
normalization helpers (`normalize_ref`, `parse_order_date`,
`parse_payment_date`) and row dataclasses, and catches per-row failures
(missing required field, unparseable amount, bad date format) instead of
letting them propagate.

`load_orders`/`load_payments` are pure -- no DB or HTTP imports -- so the
accept/reject decision is unit-testable without a live database.
`insert_orders`/`insert_payments` take an already-connected asyncpg
connection and are unit-testable with a fake connection that records its
`executemany` calls.
"""

from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import IO, Iterable, Union

import asyncpg

from app.ingest.parsing import (
    OrderRow,
    PaymentRow,
    normalize_ref,
    parse_order_date,
    parse_payment_date,
)

CSVSource = Union[IO[str], Iterable[str]]


@dataclass(frozen=True, slots=True)
class Rejection:
    row: int  # 1-based data row number (header row excluded)
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"row": self.row, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class LoadResult:
    rows: list  # list[OrderRow] or list[PaymentRow], in file order
    rejections: list[Rejection]

    @property
    def rows_loaded(self) -> int:
        return len(self.rows)

    @property
    def rows_rejected(self) -> int:
        return len(self.rejections)


def _is_blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def _optional_str(value: str | None) -> str | None:
    return None if _is_blank(value) else value


def _required_str(row: dict[str, str | None], field: str) -> str:
    value = row.get(field)
    if _is_blank(value):
        raise ValueError(f"missing {field}")
    return value.strip()


def _required_decimal(row: dict[str, str | None], field: str) -> Decimal:
    value = row.get(field)
    if _is_blank(value):
        raise ValueError(f"missing {field}")
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"unparseable {field}: {value!r}") from exc


def _optional_decimal(row: dict[str, str | None], field: str, default: Decimal) -> Decimal:
    value = row.get(field)
    if _is_blank(value):
        return default
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"unparseable {field}: {value!r}") from exc


def load_orders(file: CSVSource) -> LoadResult:
    """Parse+validate an orders CSV, row by row, tolerating per-row failures.

    A row is rejected (not raised) when a required field is missing or an
    amount/date fails to parse. A null/blank `customer_email` is not a
    rejection reason -- that's explicitly tolerated per Step 5a.
    """
    rows: list[OrderRow] = []
    rejections: list[Rejection] = []
    for i, raw in enumerate(csv.DictReader(file), start=1):
        try:
            order_id = _required_str(raw, "order_id")
            currency = _required_str(raw, "currency")
            status = _required_str(raw, "status")
            gross_amount = _required_decimal(raw, "gross_amount")
            discount = _optional_decimal(raw, "discount", Decimal("0"))
            net_amount = _required_decimal(raw, "net_amount")
            order_date = parse_order_date(raw.get("order_date"))
        except ValueError as exc:
            rejections.append(Rejection(row=i, reason=str(exc)))
            continue
        rows.append(
            OrderRow(
                order_id=order_id,
                order_id_norm=normalize_ref(order_id),
                order_date=order_date,
                customer_email=_optional_str(raw.get("customer_email")),
                currency=currency,
                gross_amount=gross_amount,
                discount=discount,
                net_amount=net_amount,
                status=status,
            )
        )
    return LoadResult(rows=rows, rejections=rejections)


def load_payments(file: CSVSource) -> LoadResult:
    """Parse+validate a payments CSV, row by row, tolerating per-row failures.

    A row is rejected (not raised) when a required field is missing or an
    amount/date fails to parse. A null/blank `processed_at` is not a
    rejection reason -- that's explicitly tolerated per Step 5a.
    """
    rows: list[PaymentRow] = []
    rejections: list[Rejection] = []
    for i, raw in enumerate(csv.DictReader(file), start=1):
        try:
            transaction_ref = _required_str(raw, "transaction_ref")
            order_reference = _required_str(raw, "order_reference")
            currency = _required_str(raw, "currency")
            payment_type = _required_str(raw, "type")
            status = _required_str(raw, "status")
            amount = _required_decimal(raw, "amount")
            fee = _optional_decimal(raw, "fee", Decimal("0"))
            net_settled = raw.get("net_settled")
            net_settled = (
                None
                if _is_blank(net_settled)
                else _required_decimal(raw, "net_settled")
            )
            processed_at = parse_payment_date(raw.get("processed_at"))
        except ValueError as exc:
            rejections.append(Rejection(row=i, reason=str(exc)))
            continue
        rows.append(
            PaymentRow(
                transaction_ref=transaction_ref,
                processed_at=processed_at,
                order_reference=order_reference,
                order_reference_norm=normalize_ref(order_reference),
                currency=currency,
                amount=amount,
                fee=fee,
                net_settled=net_settled,
                type=payment_type,
                status=status,
            )
        )
    return LoadResult(rows=rows, rejections=rejections)


_INSERT_ORDERS_SQL = """
    insert into orders (
        user_id, order_id, order_id_norm, order_date, customer_email,
        currency, gross_amount, discount, net_amount, status, upload_batch_id
    ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""

_INSERT_PAYMENTS_SQL = """
    insert into payments (
        user_id, transaction_ref, processed_at, order_reference,
        order_reference_norm, currency, amount, fee, net_settled, type,
        status, upload_batch_id
    ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
"""


async def insert_orders(
    connection: asyncpg.Connection,
    rows: list[OrderRow],
    *,
    user_id: str | uuid.UUID,
    upload_batch_id: uuid.UUID,
) -> None:
    """Bulk-insert parsed+normalized order rows for one upload batch."""
    if not rows:
        return
    user_uuid = uuid.UUID(str(user_id))
    values = [
        (
            user_uuid,
            row.order_id,
            row.order_id_norm,
            row.order_date,
            row.customer_email,
            row.currency,
            row.gross_amount,
            row.discount,
            row.net_amount,
            row.status,
            upload_batch_id,
        )
        for row in rows
    ]
    await connection.executemany(_INSERT_ORDERS_SQL, values)


async def insert_payments(
    connection: asyncpg.Connection,
    rows: list[PaymentRow],
    *,
    user_id: str | uuid.UUID,
    upload_batch_id: uuid.UUID,
) -> None:
    """Bulk-insert parsed+normalized payment rows for one upload batch."""
    if not rows:
        return
    user_uuid = uuid.UUID(str(user_id))
    values = [
        (
            user_uuid,
            row.transaction_ref,
            row.processed_at,
            row.order_reference,
            row.order_reference_norm,
            row.currency,
            row.amount,
            row.fee,
            row.net_settled,
            row.type,
            row.status,
            upload_batch_id,
        )
        for row in rows
    ]
    await connection.executemany(_INSERT_PAYMENTS_SQL, values)
