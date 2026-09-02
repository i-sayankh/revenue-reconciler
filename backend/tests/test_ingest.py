"""Tests for backend/app/ingest/loader.py and backend/app/routers/ingest.py.

`load_orders`/`load_payments` are pure -- exercised directly against small
in-memory CSV text, covering every rejection case called out in the brief
(missing required field, unparseable amount, bad date) plus the
explicitly-tolerated null `customer_email`/`processed_at` cases from Step 5a.

`insert_orders`/`insert_payments` are exercised against a fake asyncpg
connection (same style as backend/tests/test_db.py) that records its
`executemany` calls, proving the SQL/argument shaping without a live
database.

The HTTP routes are exercised with FastAPI's TestClient against a minimal
app that only mounts the ingest router, with `get_current_user_id`
(Step 4) and `get_connection` (Step 3) overridden via
`app.dependency_overrides` -- this proves the routes are wired correctly
(auth enforced, response shape correct, insert invoked) without a real
Supabase JWT or a live Postgres connection. The live acceptance check (the
real orders.csv/payments.csv through a running backend with a real bearer
token) is separate and out of scope here.
"""

from __future__ import annotations

import io
import uuid
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import get_current_user_id, get_settings
from app.db import get_connection
from app.ingest.loader import insert_orders, insert_payments, load_orders, load_payments
from app.routers import ingest as ingest_router

ORDERS_HEADER = "order_id,order_date,customer_email,currency,gross_amount,discount,net_amount,status\n"
PAYMENTS_HEADER = (
    "transaction_ref,processed_at,order_reference,currency,amount,fee,net_settled,type,status\n"
)


# -- load_orders --------------------------------------------------------


def test_load_orders_accepts_valid_rows():
    csv_text = ORDERS_HEADER + "ORD-1,2025-06-01 10:00:00,a@example.com,USD,100.00,0,100.00,completed\n"

    result = load_orders(io.StringIO(csv_text))

    assert result.rows_loaded == 1
    assert result.rows_rejected == 0
    row = result.rows[0]
    assert row.order_id_norm == "ORD-1"
    assert row.gross_amount == Decimal("100.00")


def test_load_orders_rejects_missing_order_id():
    csv_text = ORDERS_HEADER + ",2025-06-01 10:00:00,a@example.com,USD,100.00,0,100.00,completed\n"

    result = load_orders(io.StringIO(csv_text))

    assert result.rows_loaded == 0
    assert result.rows_rejected == 1
    assert result.rejections[0].row == 1
    assert "order_id" in result.rejections[0].reason


def test_load_orders_rejects_unparseable_amount():
    csv_text = ORDERS_HEADER + "ORD-2,2025-06-01 10:00:00,a@example.com,USD,not-a-number,0,100.00,completed\n"

    result = load_orders(io.StringIO(csv_text))

    assert result.rows_loaded == 0
    assert result.rows_rejected == 1
    assert "gross_amount" in result.rejections[0].reason


def test_load_orders_rejects_bad_date_format():
    csv_text = ORDERS_HEADER + "ORD-3,not-a-date,a@example.com,USD,100.00,0,100.00,completed\n"

    result = load_orders(io.StringIO(csv_text))

    assert result.rows_loaded == 0
    assert result.rows_rejected == 1


def test_load_orders_tolerates_null_customer_email():
    csv_text = ORDERS_HEADER + "ORD-4,2025-06-01 10:00:00,,USD,100.00,0,100.00,completed\n"

    result = load_orders(io.StringIO(csv_text))

    assert result.rows_loaded == 1
    assert result.rows[0].customer_email is None


def test_load_orders_tolerates_null_order_date():
    csv_text = ORDERS_HEADER + "ORD-5,,a@example.com,USD,100.00,0,100.00,completed\n"

    result = load_orders(io.StringIO(csv_text))

    assert result.rows_loaded == 1
    assert result.rows[0].order_date is None


def test_load_orders_continues_after_a_bad_row():
    csv_text = (
        ORDERS_HEADER
        + ",2025-06-01 10:00:00,a@example.com,USD,100.00,0,100.00,completed\n"
        + "ORD-6,2025-06-01 10:00:00,b@example.com,USD,50.00,0,50.00,completed\n"
    )

    result = load_orders(io.StringIO(csv_text))

    assert result.rows_loaded == 1
    assert result.rows_rejected == 1
    assert result.rows[0].order_id == "ORD-6"
    assert result.rejections[0].row == 1


def test_load_orders_defaults_missing_discount_to_zero():
    csv_text = ORDERS_HEADER + "ORD-7,2025-06-01 10:00:00,a@example.com,USD,100.00,,100.00,completed\n"

    result = load_orders(io.StringIO(csv_text))

    assert result.rows_loaded == 1
    assert result.rows[0].discount == Decimal("0")


# -- load_payments --------------------------------------------------------


def test_load_payments_accepts_valid_rows():
    csv_text = PAYMENTS_HEADER + "TXN-1,01/06/2025 10:05,ORD-1,USD,100.00,3.00,97.00,charge,settled\n"

    result = load_payments(io.StringIO(csv_text))

    assert result.rows_loaded == 1
    assert result.rows_rejected == 0
    row = result.rows[0]
    assert row.order_reference_norm == "ORD-1"
    assert row.net_settled == Decimal("97.00")


def test_load_payments_rejects_missing_transaction_ref():
    csv_text = PAYMENTS_HEADER + ",01/06/2025 10:05,ORD-1,USD,100.00,3.00,97.00,charge,settled\n"

    result = load_payments(io.StringIO(csv_text))

    assert result.rows_loaded == 0
    assert result.rows_rejected == 1
    assert "transaction_ref" in result.rejections[0].reason


def test_load_payments_rejects_missing_order_reference():
    csv_text = PAYMENTS_HEADER + "TXN-2,01/06/2025 10:05,,USD,100.00,3.00,97.00,charge,settled\n"

    result = load_payments(io.StringIO(csv_text))

    assert result.rows_loaded == 0
    assert result.rows_rejected == 1
    assert "order_reference" in result.rejections[0].reason


def test_load_payments_rejects_unparseable_amount():
    csv_text = PAYMENTS_HEADER + "TXN-3,01/06/2025 10:05,ORD-1,USD,garbage,3.00,97.00,charge,settled\n"

    result = load_payments(io.StringIO(csv_text))

    assert result.rows_loaded == 0
    assert result.rows_rejected == 1
    assert "amount" in result.rejections[0].reason


def test_load_payments_tolerates_null_processed_at():
    csv_text = PAYMENTS_HEADER + "TXN-4,,ORD-1,USD,100.00,3.00,97.00,charge,settled\n"

    result = load_payments(io.StringIO(csv_text))

    assert result.rows_loaded == 1
    assert result.rows[0].processed_at is None


def test_load_payments_tolerates_null_net_settled():
    csv_text = PAYMENTS_HEADER + "TXN-5,01/06/2025 10:05,ORD-1,USD,100.00,3.00,,charge,settled\n"

    result = load_payments(io.StringIO(csv_text))

    assert result.rows_loaded == 1
    assert result.rows[0].net_settled is None


def test_load_payments_defaults_missing_fee_to_zero():
    csv_text = PAYMENTS_HEADER + "TXN-6,01/06/2025 10:05,ORD-1,USD,100.00,,97.00,charge,settled\n"

    result = load_payments(io.StringIO(csv_text))

    assert result.rows_loaded == 1
    assert result.rows[0].fee == Decimal("0")


# -- insert_orders / insert_payments (fake connection, no live DB) --------


class _FakeConnection:
    def __init__(self):
        self.calls: list[tuple[str, list[tuple]]] = []

    async def executemany(self, sql, values):
        self.calls.append((sql, values))


async def test_insert_orders_passes_user_id_and_batch_id():
    result = load_orders(
        io.StringIO(ORDERS_HEADER + "ORD-1,2025-06-01 10:00:00,a@example.com,USD,100.00,0,100.00,completed\n")
    )
    connection = _FakeConnection()
    user_id = str(uuid.uuid4())
    batch_id = uuid.uuid4()

    await insert_orders(connection, result.rows, user_id=user_id, upload_batch_id=batch_id)

    assert len(connection.calls) == 1
    sql, values = connection.calls[0]
    assert "insert into orders" in sql
    assert len(values) == 1
    row_values = values[0]
    assert row_values[0] == uuid.UUID(user_id)
    assert row_values[1] == "ORD-1"
    assert row_values[-1] == batch_id


async def test_insert_orders_is_noop_for_empty_rows():
    connection = _FakeConnection()

    await insert_orders(connection, [], user_id=str(uuid.uuid4()), upload_batch_id=uuid.uuid4())

    assert connection.calls == []


async def test_insert_payments_passes_user_id_and_batch_id():
    result = load_payments(
        io.StringIO(PAYMENTS_HEADER + "TXN-1,01/06/2025 10:05,ORD-1,USD,100.00,3.00,97.00,charge,settled\n")
    )
    connection = _FakeConnection()
    user_id = str(uuid.uuid4())
    batch_id = uuid.uuid4()

    await insert_payments(connection, result.rows, user_id=user_id, upload_batch_id=batch_id)

    assert len(connection.calls) == 1
    sql, values = connection.calls[0]
    assert "insert into payments" in sql
    row_values = values[0]
    assert row_values[0] == uuid.UUID(user_id)
    assert row_values[1] == "TXN-1"
    assert row_values[-1] == batch_id


async def test_insert_payments_is_noop_for_empty_rows():
    connection = _FakeConnection()

    await insert_payments(connection, [], user_id=str(uuid.uuid4()), upload_batch_id=uuid.uuid4())

    assert connection.calls == []


# -- HTTP routes (dependency overrides -- no real DB or JWT needed) -------


def _make_test_app(fake_connection: _FakeConnection, user_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(ingest_router.router, prefix="/api")
    app.dependency_overrides[get_current_user_id] = lambda: user_id

    async def _fake_get_connection():
        yield fake_connection

    app.dependency_overrides[get_connection] = _fake_get_connection
    return app


def test_ingest_orders_route_returns_expected_shape_and_inserts():
    fake_connection = _FakeConnection()
    app = _make_test_app(fake_connection, str(uuid.uuid4()))
    client = TestClient(app)
    csv_bytes = (
        ORDERS_HEADER
        + "ORD-1,2025-06-01 10:00:00,a@example.com,USD,100.00,0,100.00,completed\n"
        + ",2025-06-01 10:00:00,a@example.com,USD,100.00,0,100.00,completed\n"
    ).encode("utf-8")

    response = client.post("/api/ingest/orders", files={"file": ("orders.csv", csv_bytes, "text/csv")})

    assert response.status_code == 200
    body = response.json()
    assert body["rows_loaded"] == 1
    assert body["rows_rejected"] == 1
    assert body["rejections"] == [{"row": 2, "reason": "missing order_id"}]
    assert len(fake_connection.calls) == 1


def test_ingest_payments_route_returns_expected_shape_and_inserts():
    fake_connection = _FakeConnection()
    app = _make_test_app(fake_connection, str(uuid.uuid4()))
    client = TestClient(app)
    csv_bytes = (
        PAYMENTS_HEADER + "TXN-1,01/06/2025 10:05,ORD-1,USD,100.00,3.00,97.00,charge,settled\n"
    ).encode("utf-8")

    response = client.post("/api/ingest/payments", files={"file": ("payments.csv", csv_bytes, "text/csv")})

    assert response.status_code == 200
    body = response.json()
    assert body["rows_loaded"] == 1
    assert body["rows_rejected"] == 0
    assert body["rejections"] == []
    assert len(fake_connection.calls) == 1


def test_ingest_orders_route_skips_insert_when_all_rows_rejected():
    fake_connection = _FakeConnection()
    app = _make_test_app(fake_connection, str(uuid.uuid4()))
    client = TestClient(app)
    csv_bytes = (ORDERS_HEADER + ",2025-06-01 10:00:00,a@example.com,USD,100.00,0,100.00,completed\n").encode(
        "utf-8"
    )

    response = client.post("/api/ingest/orders", files={"file": ("orders.csv", csv_bytes, "text/csv")})

    assert response.status_code == 200
    body = response.json()
    assert body["rows_loaded"] == 0
    assert body["rows_rejected"] == 1
    assert fake_connection.calls == []


def test_ingest_orders_route_requires_auth():
    app = FastAPI()
    app.include_router(ingest_router.router, prefix="/api")
    # get_current_user_id itself is intentionally not overridden -- only its
    # settings sub-dependency is, so the process doesn't need a real .env.
    # No Authorization header must fail with 401 before any parsing happens.
    app.dependency_overrides[get_settings] = lambda: object()
    client = TestClient(app)
    csv_bytes = (ORDERS_HEADER + "ORD-1,2025-06-01 10:00:00,a@example.com,USD,100.00,0,100.00,completed\n").encode(
        "utf-8"
    )

    response = client.post("/api/ingest/orders", files={"file": ("orders.csv", csv_bytes, "text/csv")})

    assert response.status_code == 401
