"""Tests for backend/app/ingest/parsing.py.

Uses the mini_orders.csv / mini_payments.csv fixtures under
backend/tests/fixtures, which were built specifically to exercise every
quirk called out in the plan's data-quality findings: both date formats,
a case/whitespace-mangled ref, a null email, a null timestamp, and an
exact-duplicate order row. Assertions here stop at "parsed, normalized,
deduped rows" -- discrepancy classification is Step 6's job.
"""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.ingest.parsing import (
    normalize_ref,
    parse_order_date,
    parse_orders_csv,
    parse_payment_date,
    parse_payments_csv,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ORDERS_CSV = FIXTURES_DIR / "mini_orders.csv"
PAYMENTS_CSV = FIXTURES_DIR / "mini_payments.csv"


# -- normalize_ref -----------------------------------------------------


def test_normalize_ref_strips_and_uppercases():
    assert normalize_ref(" fix-1012 ") == "FIX-1012"


def test_normalize_ref_is_idempotent_on_clean_input():
    assert normalize_ref("FIX-1001") == "FIX-1001"


# -- date parsing --------------------------------------------------------


def test_parse_order_date_format():
    assert parse_order_date("2025-06-01 10:00:00") == datetime(2025, 6, 1, 10, 0, 0)


def test_parse_order_date_blank_is_none():
    assert parse_order_date("") is None
    assert parse_order_date(None) is None


def test_parse_payment_date_format():
    assert parse_payment_date("01/06/2025 10:05") == datetime(2025, 6, 1, 10, 5)


def test_parse_payment_date_blank_is_none():
    assert parse_payment_date("") is None
    assert parse_payment_date(None) is None


# -- parse_orders_csv ------------------------------------------------------


def test_parse_orders_csv_dedups_exact_duplicate_row():
    rows = parse_orders_csv(ORDERS_CSV)
    # 15 lines in the fixture, one is an exact duplicate of FIX-1013.
    assert len(rows) == 14
    fix_1013_rows = [r for r in rows if r.order_id == "FIX-1013"]
    assert len(fix_1013_rows) == 1


def test_parse_orders_csv_normalizes_ref():
    rows = parse_orders_csv(ORDERS_CSV)
    row = next(r for r in rows if r.order_id == "FIX-1001")
    assert row.order_id_norm == "FIX-1001"


def test_parse_orders_csv_parses_dates_and_amounts():
    rows = parse_orders_csv(ORDERS_CSV)
    row = next(r for r in rows if r.order_id == "FIX-1005")
    assert row.order_date == datetime(2025, 6, 3, 8, 0, 0)
    assert row.gross_amount == Decimal("105.00")
    assert row.discount == Decimal("5.00")
    assert row.net_amount == Decimal("100.00")
    assert row.currency == "USD"
    assert row.status == "completed"


def test_parse_orders_csv_null_email_preserved_as_none():
    rows = parse_orders_csv(ORDERS_CSV)
    row = next(r for r in rows if r.order_id == "FIX-1014")
    assert row.customer_email is None
    # the row itself must not be dropped
    assert row.gross_amount == Decimal("55.00")


def test_parse_orders_csv_accepts_open_file_handle():
    with open(ORDERS_CSV, newline="", encoding="utf-8") as f:
        rows = parse_orders_csv(f)
    assert len(rows) == 14


def test_parse_orders_csv_blank_discount_defaults_to_zero(tmp_path):
    # Real-world bug: sample-data/orders.csv has a row with a blank
    # discount field (schema default is 0), which used to crash the
    # parser (Decimal("")).
    csv_path = tmp_path / "orders_blank_discount.csv"
    csv_path.write_text(
        "order_id,order_date,customer_email,currency,gross_amount,discount,net_amount,status\n"
        "ORD-2201,2025-05-19 00:00:00,,USD,120.0,,120.0,completed\n",
        encoding="utf-8",
    )
    rows = parse_orders_csv(csv_path)
    assert len(rows) == 1
    assert rows[0].discount == Decimal("0")
    assert rows[0].gross_amount == Decimal("120.0")
    assert rows[0].customer_email is None


# -- parse_payments_csv ------------------------------------------------------


def test_parse_payments_csv_row_count():
    rows = parse_payments_csv(PAYMENTS_CSV)
    # 17 data rows in the fixture, no exact duplicates among them.
    assert len(rows) == 17


def test_parse_payments_csv_normalizes_padded_lowercase_ref():
    rows = parse_payments_csv(PAYMENTS_CSV)
    row = next(r for r in rows if r.transaction_ref == "TXN-FIX-012")
    assert row.order_reference == " fix-1012 "
    assert row.order_reference_norm == "FIX-1012"


def test_parse_payments_csv_parses_dates_and_amounts():
    rows = parse_payments_csv(PAYMENTS_CSV)
    row = next(r for r in rows if r.transaction_ref == "TXN-FIX-001")
    assert row.processed_at == datetime(2025, 6, 1, 10, 5)
    assert row.amount == Decimal("210.00")
    assert row.fee == Decimal("6.30")
    assert row.net_settled == Decimal("203.70")
    assert row.order_reference_norm == "FIX-1001"
    assert row.type == "charge"
    assert row.status == "settled"


def test_parse_payments_csv_null_processed_at_preserved_as_none():
    rows = parse_payments_csv(PAYMENTS_CSV)
    row = next(r for r in rows if r.transaction_ref == "TXN-FIX-014")
    assert row.processed_at is None
    # the row itself must not be dropped
    assert row.order_reference_norm == "FIX-1014"


def test_parse_payments_csv_unmatched_reference_parses_fine():
    # FIX-1099 doesn't exist in the orders fixture -- matching it is
    # Step 6's job, the parser just needs to parse the row.
    rows = parse_payments_csv(PAYMENTS_CSV)
    row = next(r for r in rows if r.transaction_ref == "TXN-FIX-099")
    assert row.order_reference_norm == "FIX-1099"
    assert row.amount == Decimal("77.00")


def test_parse_payments_csv_accepts_open_file_handle():
    with open(PAYMENTS_CSV, newline="", encoding="utf-8") as f:
        rows = parse_payments_csv(f)
    assert len(rows) == 17


def test_parse_payments_csv_blank_fee_defaults_to_zero(tmp_path):
    # fee shares discount's schema shape (numeric(12,2) not null default
    # 0), so it gets the same blank-tolerant treatment even though no
    # blank fee has been observed in real data yet.
    csv_path = tmp_path / "payments_blank_fee.csv"
    csv_path.write_text(
        "transaction_ref,processed_at,order_reference,currency,amount,fee,net_settled,type,status\n"
        "TXN-BLANKFEE,01/06/2025 10:05,ORD-9999,USD,120.0,,120.0,charge,settled\n",
        encoding="utf-8",
    )
    rows = parse_payments_csv(csv_path)
    assert len(rows) == 1
    assert rows[0].fee == Decimal("0")
    assert rows[0].amount == Decimal("120.0")
