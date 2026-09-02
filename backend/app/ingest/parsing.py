"""CSV parsing and normalization for the two source files: orders and payments.

Pure functions -- no DB or HTTP imports. Row values are normalized here
(ref casing/whitespace, date parsing, decimal amounts) so that everything
downstream (Step 5b's DB loader, Step 6's engine) works with clean, typed
data instead of raw CSV strings.

Dedup: exact-duplicate-row dedup (the id/ref column and every other field
byte-for-byte identical) is applied inside each `parse_*_csv` function,
on the raw rows, before normalization -- first occurrence wins, later
duplicates are dropped silently. This keeps both parsers' guarantees
identical, even though real-world duplicates are expected mainly in
order exports.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import IO, Iterable, Union

ORDER_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
PAYMENT_DATE_FORMAT = "%d/%m/%Y %H:%M"

CSVSource = Union[str, Path, IO[str], Iterable[str]]


@dataclass(frozen=True, slots=True)
class OrderRow:
    order_id: str
    order_id_norm: str
    order_date: datetime | None
    customer_email: str | None
    currency: str
    gross_amount: Decimal
    discount: Decimal
    net_amount: Decimal
    status: str


@dataclass(frozen=True, slots=True)
class PaymentRow:
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


def normalize_ref(s: str) -> str:
    """Normalize an order id / order reference for cross-file matching."""
    return s.strip().upper()


def _is_blank(s: str | None) -> bool:
    return s is None or s.strip() == ""


def parse_order_date(s: str | None) -> datetime | None:
    """Parse an orders-CSV date: `YYYY-MM-DD HH:MM:SS`. Blank -> None."""
    if _is_blank(s):
        return None
    return datetime.strptime(s.strip(), ORDER_DATE_FORMAT)


def parse_payment_date(s: str | None) -> datetime | None:
    """Parse a payments-CSV date: `DD/MM/YYYY HH:MM`. Blank -> None."""
    if _is_blank(s):
        return None
    return datetime.strptime(s.strip(), PAYMENT_DATE_FORMAT)


def _optional_decimal(s: str | None) -> Decimal | None:
    if _is_blank(s):
        return None
    return Decimal(s.strip())


def _decimal_default_zero(s: str | None) -> Decimal:
    """Parse a decimal field that defaults to 0 when blank.

    Used for `discount` and `fee`, the two amount columns whose schema
    definition is `numeric(12,2) not null default 0` -- a blank value in
    the source CSV means "no discount/fee charged", not missing data, so
    it is safe (and matches the DB default) to treat it as `Decimal("0")`
    instead of raising.
    """
    if _is_blank(s):
        return Decimal("0")
    return Decimal(s.strip())


def _optional_str(s: str | None) -> str | None:
    return None if _is_blank(s) else s


def _dedup_exact_rows(rows: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    """Drop byte-for-byte duplicate rows, keeping the first occurrence.

    A "duplicate" means every field in the row is identical -- not just a
    matching id/ref with different amounts.
    """
    seen: set[tuple[tuple[str, str | None], ...]] = set()
    deduped: list[dict[str, str | None]] = []
    for row in rows:
        key = tuple(sorted(row.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _read_rows(file: CSVSource) -> list[dict[str, str | None]]:
    if isinstance(file, (str, Path)):
        with open(file, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    return list(csv.DictReader(file))


def parse_orders_csv(file: CSVSource) -> list[OrderRow]:
    """Parse+normalize the orders CSV into `OrderRow`s.

    Exact-duplicate rows are deduped before normalization, keeping the
    first occurrence. A null/empty `customer_email` is preserved as
    `None` rather than crashing or dropping the row. A blank `discount`
    parses as `Decimal("0")` (matching the column's schema default)
    rather than crashing.
    """
    rows = _dedup_exact_rows(_read_rows(file))
    result: list[OrderRow] = []
    for row in rows:
        order_id = row["order_id"] or ""
        result.append(
            OrderRow(
                order_id=order_id,
                order_id_norm=normalize_ref(order_id),
                order_date=parse_order_date(row.get("order_date")),
                customer_email=_optional_str(row.get("customer_email")),
                currency=row["currency"] or "",
                gross_amount=Decimal(row["gross_amount"]),
                discount=_decimal_default_zero(row.get("discount")),
                net_amount=Decimal(row["net_amount"]),
                status=row["status"] or "",
            )
        )
    return result


def parse_payments_csv(file: CSVSource) -> list[PaymentRow]:
    """Parse+normalize the payments CSV into `PaymentRow`s.

    Exact-duplicate rows are deduped the same way as `parse_orders_csv`
    (none are expected in real payment exports, but the guarantee is
    kept consistent between both parsers). A null/empty `processed_at`
    is preserved as `None` rather than crashing or dropping the row. A
    blank `fee` parses as `Decimal("0")` (matching the column's schema
    default), for the same reason as `discount` in `parse_orders_csv`.
    """
    rows = _dedup_exact_rows(_read_rows(file))
    result: list[PaymentRow] = []
    for row in rows:
        order_reference = row["order_reference"] or ""
        result.append(
            PaymentRow(
                transaction_ref=row["transaction_ref"] or "",
                processed_at=parse_payment_date(row.get("processed_at")),
                order_reference=order_reference,
                order_reference_norm=normalize_ref(order_reference),
                currency=row["currency"] or "",
                amount=Decimal(row["amount"]),
                fee=_decimal_default_zero(row.get("fee")),
                net_settled=_optional_decimal(row.get("net_settled")),
                type=row["type"] or "",
                status=row["status"] or "",
            )
        )
    return result
