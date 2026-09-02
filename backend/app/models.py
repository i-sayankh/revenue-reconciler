"""Plain query models for the four tables in backend/schema.sql.

These are not an ORM layer -- just typed representations of a full row as
returned by a `SELECT *`-shaped asyncpg query, used to type data flowing
into/out of the queries in the route handlers. Build one with `from_record`
from anything mapping-like (an `asyncpg.Record`, or a plain dict in tests).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, TypeVar

T = TypeVar("T", bound="_FromRecordMixin")


class _FromRecordMixin:
    """Adds a `from_record` classmethod to a dataclass, keyed off its own fields."""

    @classmethod
    def from_record(cls: type[T], record: Mapping[str, Any]) -> T:
        return cls(**{f.name: record[f.name] for f in fields(cls)})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Order(_FromRecordMixin):
    id: uuid.UUID
    user_id: uuid.UUID
    order_id: str
    order_id_norm: str
    order_date: datetime | None
    customer_email: str | None
    currency: str
    gross_amount: Decimal
    discount: Decimal
    net_amount: Decimal
    status: str
    upload_batch_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Payment(_FromRecordMixin):
    id: uuid.UUID
    user_id: uuid.UUID
    transaction_ref: str
    processed_at: datetime | None
    order_reference: str
    order_reference_norm: str
    currency: str
    amount: Decimal
    fee: Decimal
    net_settled: Decimal | None
    type: str
    status: str
    upload_batch_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationRun(_FromRecordMixin):
    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime
    orders_count: int
    payments_count: int
    total_reconciled_value: Decimal
    total_disputed_value: Decimal
    money_at_risk: Decimal
    status: str


@dataclass(frozen=True, slots=True)
class Discrepancy(_FromRecordMixin):
    id: uuid.UUID
    run_id: uuid.UUID
    user_id: uuid.UUID
    type: str
    order_id: str | None
    payment_ref: str | None
    order_amount: Decimal | None
    payment_amount: Decimal | None
    currency_order: str | None
    currency_payment: str | None
    difference: Decimal | None
    detail: dict[str, Any] | None
    explanation: dict[str, Any] | None
    explained_at: datetime | None
