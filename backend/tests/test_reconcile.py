"""Tests for backend/app/reconcile/service.py and backend/app/routers/reconcile.py.

The pure shaping functions (`build_discrepancy_inserts`, `compute_disputed_value`,
`_discrepancy_filter_clauses`) are exercised directly against the Step 5a/6
`mini_orders.csv`/`mini_payments.csv` fixtures (same fixtures as
`test_engine.py`) fed through the real `reconcile()` -- no DB involved.

The DB-touching functions (`fetch_latest_orders`/`fetch_latest_payments`/
`persist_run`/`fetch_latest_run`/`fetch_by_type_summary`/`fetch_discrepancies`)
are exercised against `_FakeConnection`, an in-memory stand-in for
`asyncpg.Connection` that recognizes the fixed query shapes this module
issues (it is not a SQL engine -- it pattern-matches each query's SQL text
and reproduces the equivalent Python behavior) and stores/retrieves plain
dicts the same way `asyncpg.Record` objects behave under `record[field]`
access. This proves the run-persistence and query-filtering/pagination
logic end to end without a live Postgres connection.

The HTTP routes are exercised with FastAPI's TestClient against a minimal
app that only mounts the reconcile router, with `get_current_user_id` and
`get_connection` overridden via `app.dependency_overrides` (same pattern as
`test_ingest.py`) -- backed by the same `_FakeConnection`, so a full
upload -> run -> summary -> filtered-list cycle can be proven without a
real Supabase JWT or a live database. The live acceptance check (the real
orders.csv/payments.csv through a running backend with a real bearer token)
is separate and out of scope here.

Expected numbers below (orders_count=14, payments_count=17,
total_reconciled_value=570.00, total_disputed_value=846.00,
money_at_risk=359.50, 15 discrepancy rows across 8 types) are hand-computed
from the fixture data and cross-checked against `test_engine.py`'s
per-order classification table.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import get_current_user_id, get_settings
from app.db import get_connection
from app.engine.reconcile import reconcile
from app.ingest.parsing import OrderRow, PaymentRow, parse_orders_csv, parse_payments_csv
from app.reconcile import service
from app.routers import reconcile as reconcile_router

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ORDERS_CSV = FIXTURES_DIR / "mini_orders.csv"
PAYMENTS_CSV = FIXTURES_DIR / "mini_payments.csv"


# -- pure shaping logic (no DB) -------------------------------------------


@pytest.fixture(scope="module")
def engine_result():
    orders = parse_orders_csv(ORDERS_CSV)
    payments = parse_payments_csv(PAYMENTS_CSV)
    return reconcile(orders, payments)


def test_engine_result_headline_numbers_match_hand_computed_totals(engine_result):
    """Locks in the numbers every other assertion in this file depends on."""
    assert engine_result.reconciled_value == Decimal("570.00")
    assert engine_result.money_at_risk == Decimal("359.50")
    assert len(engine_result.order_discrepancies) == 14
    assert len(engine_result.orphan_payments) == 1


def test_compute_disputed_value_matches_expected(engine_result):
    assert service.compute_disputed_value(engine_result) == Decimal("846.00")


def test_build_discrepancy_inserts_row_count(engine_result):
    inserts = service.build_discrepancy_inserts(engine_result)
    assert len(inserts) == 15  # 14 orders (incl. RECONCILED) + 1 orphan payment


def test_build_discrepancy_inserts_duplicate_charge_lists_both_refs(engine_result):
    inserts = service.build_discrepancy_inserts(engine_result)
    dup = next(i for i in inserts if i.order_id == "FIX-1003")
    assert dup.type == "DUPLICATE_CHARGE"
    assert sorted(dup.detail["matched_payment_refs"]) == ["TXN-FIX-003A", "TXN-FIX-003B"]


def test_build_discrepancy_inserts_missing_payment_has_no_payment_side(engine_result):
    inserts = service.build_discrepancy_inserts(engine_result)
    missing = next(i for i in inserts if i.order_id == "FIX-1002")
    assert missing.type == "MISSING_PAYMENT"
    assert missing.payment_ref is None
    assert missing.payment_amount is None
    assert missing.currency_payment is None


def test_build_discrepancy_inserts_unsettled_payment_uses_the_unsettled_charge(engine_result):
    inserts = service.build_discrepancy_inserts(engine_result)
    row = next(i for i in inserts if i.order_id == "FIX-1007")
    assert row.type == "UNSETTLED_PAYMENT"
    assert row.payment_ref == "TXN-FIX-007"
    assert row.payment_amount == Decimal("60.00")


def test_build_discrepancy_inserts_status_contradiction_picks_settled_charge_not_refund(engine_result):
    inserts = service.build_discrepancy_inserts(engine_result)
    row = next(i for i in inserts if i.order_id == "FIX-1010")
    assert row.type == "STATUS_CONTRADICTION"
    assert row.payment_ref == "TXN-FIX-010A"
    assert row.detail["status_contradiction_reason"] == "completed_but_refunded"


def test_build_discrepancy_inserts_orphan_payment_row(engine_result):
    inserts = service.build_discrepancy_inserts(engine_result)
    orphan = next(i for i in inserts if i.type == "ORPHAN_PAYMENT")
    assert orphan.order_id is None
    assert orphan.order_amount is None
    assert orphan.payment_ref == "TXN-FIX-099"
    assert orphan.payment_amount == Decimal("77.00")
    assert orphan.detail["order_reference"] == "FIX-1099"


def test_build_discrepancy_inserts_reconciled_row_marks_verified_reason(engine_result):
    inserts = service.build_discrepancy_inserts(engine_result)
    row = next(i for i in inserts if i.order_id == "FIX-1001")
    assert row.type == "RECONCILED"
    assert row.detail["reconciled_reason"] == "verified"


# -- _discrepancy_filter_clauses (pure SQL/params construction) ----------


def test_filter_clauses_base_case_only_run_id():
    run_id = uuid.uuid4()
    clause, params = service._discrepancy_filter_clauses(
        run_id=run_id, type_=None, q=None, min_amount=None, max_amount=None
    )
    assert clause == "run_id = $1"
    assert params == [run_id]


def test_filter_clauses_all_filters_combined_in_order():
    run_id = uuid.uuid4()
    clause, params = service._discrepancy_filter_clauses(
        run_id=run_id,
        type_="AMOUNT_MISMATCH",
        q="ord-1",
        min_amount=Decimal("10"),
        max_amount=Decimal("500"),
    )
    assert clause == (
        "run_id = $1 and type = $2 and (order_id ilike $3 or payment_ref ilike $3) "
        "and order_amount >= $4 and order_amount <= $5"
    )
    assert params == [run_id, "AMOUNT_MISMATCH", "%ord-1%", Decimal("10"), Decimal("500")]


# -- _FakeConnection: in-memory stand-in for asyncpg.Connection ----------


class _FakeConnection:
    """Recognizes the fixed query shapes app.reconcile.service issues and
    reproduces their behavior against in-memory dict "tables" -- not a SQL
    engine, but enough to prove the persistence/query logic without Postgres.
    """

    def __init__(self):
        self.orders: list[dict[str, Any]] = []
        self.payments: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.discrepancies: list[dict[str, Any]] = []

    @staticmethod
    def _norm(sql: str) -> str:
        return " ".join(sql.split()).lower()

    @staticmethod
    def _latest_batch(table: list[dict[str, Any]], user_id: Any) -> Any:
        candidates = [r for r in table if r["user_id"] == user_id]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r["created_at"])["upload_batch_id"]

    def _filter_discrepancies(self, norm_sql: str, params: tuple) -> tuple[list[dict[str, Any]], int]:
        run_id = params[0]
        rows = [r for r in self.discrepancies if r["run_id"] == run_id]
        idx = 1
        if " type = $" in norm_sql:
            rows = [r for r in rows if r["type"] == params[idx]]
            idx += 1
        if "ilike" in norm_sql:
            needle = params[idx].strip("%").lower()
            idx += 1
            rows = [
                r
                for r in rows
                if (r["order_id"] and needle in r["order_id"].lower())
                or (r["payment_ref"] and needle in r["payment_ref"].lower())
            ]
        if "order_amount >= $" in norm_sql:
            bound = params[idx]
            idx += 1
            rows = [r for r in rows if r["order_amount"] is not None and r["order_amount"] >= bound]
        if "order_amount <= $" in norm_sql:
            bound = params[idx]
            idx += 1
            rows = [r for r in rows if r["order_amount"] is not None and r["order_amount"] <= bound]
        return rows, idx

    async def fetch(self, sql, *params):
        norm = self._norm(sql)
        if norm.startswith("select * from orders"):
            user_id = params[0]
            batch = self._latest_batch(self.orders, user_id)
            return [r for r in self.orders if r["user_id"] == user_id and r["upload_batch_id"] == batch]
        if norm.startswith("select * from payments"):
            user_id = params[0]
            batch = self._latest_batch(self.payments, user_id)
            return [r for r in self.payments if r["user_id"] == user_id and r["upload_batch_id"] == batch]
        if norm.startswith("select type,"):
            run_id = params[0]
            rows = [r for r in self.discrepancies if r["run_id"] == run_id]
            agg: dict[str, dict[str, Any]] = {}
            for r in rows:
                value = r["order_amount"] if r["order_amount"] is not None else r["payment_amount"]
                bucket = agg.setdefault(r["type"], {"type": r["type"], "count": 0, "value": Decimal("0")})
                bucket["count"] += 1
                bucket["value"] += value or Decimal("0")
            return [agg[t] for t in sorted(agg)]
        if norm.startswith("select * from discrepancies where"):
            filtered, idx = self._filter_discrepancies(norm, params)
            limit = params[idx]
            offset = params[idx + 1]
            ordered = sorted(
                filtered,
                key=lambda r: (r["order_amount"] is None, -(r["order_amount"] or Decimal("0"))),
            )
            return ordered[offset : offset + limit]
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def fetchrow(self, sql, *params):
        norm = self._norm(sql)
        if norm.startswith("insert into reconciliation_runs"):
            row = {
                "id": uuid.uuid4(),
                "user_id": params[0],
                "created_at": datetime.now(timezone.utc) + timedelta(microseconds=len(self.runs)),
                "orders_count": params[1],
                "payments_count": params[2],
                "total_reconciled_value": params[3],
                "total_disputed_value": params[4],
                "money_at_risk": params[5],
                "status": params[6],
            }
            self.runs.append(row)
            return row
        if norm.startswith("select * from reconciliation_runs"):
            user_id = params[0]
            candidates = [r for r in self.runs if r["user_id"] == user_id]
            return max(candidates, key=lambda r: r["created_at"]) if candidates else None
        if norm.startswith("select count(*) as total from discrepancies"):
            filtered, _ = self._filter_discrepancies(norm, params)
            return {"total": len(filtered)}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def executemany(self, sql, values):
        norm = self._norm(sql)
        if norm.startswith("insert into discrepancies"):
            for v in values:
                self.discrepancies.append(
                    {
                        "id": uuid.uuid4(),
                        "run_id": v[0],
                        "user_id": v[1],
                        "type": v[2],
                        "order_id": v[3],
                        "payment_ref": v[4],
                        "order_amount": v[5],
                        "payment_amount": v[6],
                        "currency_order": v[7],
                        "currency_payment": v[8],
                        "difference": v[9],
                        "detail": v[10],
                        "explanation": None,
                        "explained_at": None,
                    }
                )
            return
        raise AssertionError(f"unexpected executemany SQL: {sql}")


def _order_dict(row: OrderRow, *, user_id: uuid.UUID, batch_id: uuid.UUID, created_at: datetime) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "user_id": user_id,
        "order_id": row.order_id,
        "order_id_norm": row.order_id_norm,
        "order_date": row.order_date,
        "customer_email": row.customer_email,
        "currency": row.currency,
        "gross_amount": row.gross_amount,
        "discount": row.discount,
        "net_amount": row.net_amount,
        "status": row.status,
        "upload_batch_id": batch_id,
        "created_at": created_at,
    }


def _payment_dict(row: PaymentRow, *, user_id: uuid.UUID, batch_id: uuid.UUID, created_at: datetime) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "user_id": user_id,
        "transaction_ref": row.transaction_ref,
        "processed_at": row.processed_at,
        "order_reference": row.order_reference,
        "order_reference_norm": row.order_reference_norm,
        "currency": row.currency,
        "amount": row.amount,
        "fee": row.fee,
        "net_settled": row.net_settled,
        "type": row.type,
        "status": row.status,
        "upload_batch_id": batch_id,
        "created_at": created_at,
    }


@pytest.fixture
def seeded_connection() -> tuple[_FakeConnection, str]:
    """A fresh `_FakeConnection` pre-loaded with one order batch and one
    payment batch (the mini fixtures) for a single random user."""
    user_id = uuid.uuid4()
    connection = _FakeConnection()
    now = datetime.now(timezone.utc)
    order_batch, payment_batch = uuid.uuid4(), uuid.uuid4()
    for row in parse_orders_csv(ORDERS_CSV):
        connection.orders.append(_order_dict(row, user_id=user_id, batch_id=order_batch, created_at=now))
    for row in parse_payments_csv(PAYMENTS_CSV):
        connection.payments.append(_payment_dict(row, user_id=user_id, batch_id=payment_batch, created_at=now))
    return connection, str(user_id)


# -- fetch_latest_orders / fetch_latest_payments --------------------------


async def test_fetch_latest_orders_returns_only_the_most_recent_batch():
    connection = _FakeConnection()
    user_id = uuid.uuid4()
    old_row, new_row = parse_orders_csv(ORDERS_CSV)[0], parse_orders_csv(ORDERS_CSV)[1]
    connection.orders.append(
        _order_dict(old_row, user_id=user_id, batch_id=uuid.uuid4(), created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    )
    connection.orders.append(
        _order_dict(new_row, user_id=user_id, batch_id=uuid.uuid4(), created_at=datetime(2025, 6, 1, tzinfo=timezone.utc))
    )

    result = await service.fetch_latest_orders(connection, str(user_id))

    assert len(result) == 1
    assert result[0].order_id == new_row.order_id


async def test_fetch_latest_orders_empty_when_user_never_uploaded():
    connection = _FakeConnection()
    result = await service.fetch_latest_orders(connection, str(uuid.uuid4()))
    assert result == []


async def test_fetch_latest_payments_returns_only_the_most_recent_batch():
    connection = _FakeConnection()
    user_id = uuid.uuid4()
    old_row, new_row = parse_payments_csv(PAYMENTS_CSV)[0], parse_payments_csv(PAYMENTS_CSV)[1]
    connection.payments.append(
        _payment_dict(old_row, user_id=user_id, batch_id=uuid.uuid4(), created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    )
    connection.payments.append(
        _payment_dict(new_row, user_id=user_id, batch_id=uuid.uuid4(), created_at=datetime(2025, 6, 1, tzinfo=timezone.utc))
    )

    result = await service.fetch_latest_payments(connection, str(user_id))

    assert len(result) == 1
    assert result[0].transaction_ref == new_row.transaction_ref


# -- persist_run -----------------------------------------------------------


async def test_persist_run_computes_expected_totals_and_inserts_all_rows(seeded_connection):
    connection, user_id = seeded_connection
    orders = await service.fetch_latest_orders(connection, user_id)
    payments = await service.fetch_latest_payments(connection, user_id)
    result = service.run_reconciliation(orders, payments)

    run = await service.persist_run(
        connection, user_id=user_id, result=result, orders_count=len(orders), payments_count=len(payments)
    )

    assert run.orders_count == 14
    assert run.payments_count == 17
    assert run.total_reconciled_value == Decimal("570.00")
    assert run.total_disputed_value == Decimal("846.00")
    assert run.money_at_risk == Decimal("359.50")
    assert run.status == "complete"
    assert len(connection.discrepancies) == 15
    assert all(d["run_id"] == run.id for d in connection.discrepancies)


# -- fetch_latest_run -------------------------------------------------------


async def test_fetch_latest_run_returns_none_when_no_run_yet():
    connection = _FakeConnection()
    assert await service.fetch_latest_run(connection, str(uuid.uuid4())) is None


async def test_fetch_latest_run_returns_the_most_recent_run(seeded_connection):
    connection, user_id = seeded_connection
    orders = await service.fetch_latest_orders(connection, user_id)
    payments = await service.fetch_latest_payments(connection, user_id)
    result = service.run_reconciliation(orders, payments)
    first_run = await service.persist_run(
        connection, user_id=user_id, result=result, orders_count=len(orders), payments_count=len(payments)
    )
    second_run = await service.persist_run(
        connection, user_id=user_id, result=result, orders_count=len(orders), payments_count=len(payments)
    )

    latest = await service.fetch_latest_run(connection, user_id)

    assert latest.id == second_run.id
    assert latest.id != first_run.id


# -- fetch_by_type_summary --------------------------------------------------


async def test_fetch_by_type_summary_matches_expected_breakdown(seeded_connection):
    connection, user_id = seeded_connection
    orders = await service.fetch_latest_orders(connection, user_id)
    payments = await service.fetch_latest_payments(connection, user_id)
    result = service.run_reconciliation(orders, payments)
    run = await service.persist_run(
        connection, user_id=user_id, result=result, orders_count=len(orders), payments_count=len(payments)
    )

    by_type = {b["type"]: b for b in await service.fetch_by_type_summary(connection, run.id)}

    assert sum(b["count"] for b in by_type.values()) == 15
    assert by_type["RECONCILED"] == {"type": "RECONCILED", "count": 6, "value": Decimal("570.00")}
    assert by_type["MISSING_PAYMENT"] == {"type": "MISSING_PAYMENT", "count": 1, "value": Decimal("75.00")}
    assert by_type["UNSETTLED_PAYMENT"] == {"type": "UNSETTLED_PAYMENT", "count": 2, "value": Decimal("140.00")}
    assert by_type["ORPHAN_PAYMENT"] == {"type": "ORPHAN_PAYMENT", "count": 1, "value": Decimal("77.00")}


# -- fetch_discrepancies -----------------------------------------------------


async def _persisted_run(connection, user_id):
    orders = await service.fetch_latest_orders(connection, user_id)
    payments = await service.fetch_latest_payments(connection, user_id)
    result = service.run_reconciliation(orders, payments)
    return await service.persist_run(
        connection, user_id=user_id, result=result, orders_count=len(orders), payments_count=len(payments)
    )


async def test_fetch_discrepancies_filters_by_type(seeded_connection):
    connection, user_id = seeded_connection
    run = await _persisted_run(connection, user_id)

    rows, total = await service.fetch_discrepancies(connection, run_id=run.id, type_="UNSETTLED_PAYMENT")

    assert total == 2
    assert {r.order_id for r in rows} == {"FIX-1007", "FIX-1008"}


async def test_fetch_discrepancies_filters_by_q_case_insensitive(seeded_connection):
    connection, user_id = seeded_connection
    run = await _persisted_run(connection, user_id)

    rows, total = await service.fetch_discrepancies(connection, run_id=run.id, q="fix-1009")

    assert total == 1
    assert rows[0].order_id == "FIX-1009"


async def test_fetch_discrepancies_filters_by_amount_range(seeded_connection):
    connection, user_id = seeded_connection
    run = await _persisted_run(connection, user_id)

    rows, total = await service.fetch_discrepancies(
        connection, run_id=run.id, min_amount=Decimal("90"), max_amount=Decimal("100")
    )

    assert total == 3
    assert {r.order_id for r in rows} == {"FIX-1003", "FIX-1005", "FIX-1010"}


async def test_fetch_discrepancies_excludes_null_order_amount_rows_when_range_set(seeded_connection):
    connection, user_id = seeded_connection
    run = await _persisted_run(connection, user_id)

    rows, _ = await service.fetch_discrepancies(connection, run_id=run.id, min_amount=Decimal("0"))

    assert all(r.type != "ORPHAN_PAYMENT" for r in rows)


async def test_fetch_discrepancies_paginates_without_overlap(seeded_connection):
    connection, user_id = seeded_connection
    run = await _persisted_run(connection, user_id)

    page1, total1 = await service.fetch_discrepancies(connection, run_id=run.id, page=1, page_size=5)
    page2, total2 = await service.fetch_discrepancies(connection, run_id=run.id, page=2, page_size=5)

    assert total1 == total2 == 15
    assert len(page1) == 5
    assert len(page2) == 5
    assert {r.id for r in page1}.isdisjoint({r.id for r in page2})


# -- HTTP routes (dependency overrides -- no real DB or JWT needed) -------


def _make_test_app(connection: _FakeConnection, user_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(reconcile_router.router, prefix="/api")
    app.dependency_overrides[get_current_user_id] = lambda: user_id

    async def _fake_get_connection():
        yield connection

    app.dependency_overrides[get_connection] = _fake_get_connection
    return app


def test_run_route_returns_400_when_nothing_uploaded():
    connection = _FakeConnection()
    app = _make_test_app(connection, str(uuid.uuid4()))
    client = TestClient(app)

    response = client.post("/api/reconcile/run")

    assert response.status_code == 400
    assert connection.runs == []


def test_run_route_persists_and_returns_expected_headline_totals(seeded_connection):
    connection, user_id = seeded_connection
    app = _make_test_app(connection, user_id)
    client = TestClient(app)

    response = client.post("/api/reconcile/run")

    assert response.status_code == 200
    run = response.json()["run"]
    assert run["orders_count"] == 14
    assert run["payments_count"] == 17
    # Decimal fields serialize as JSON strings (FastAPI/pydantic v2's
    # return-type-based serialization keeps full precision instead of
    # lossy float conversion) -- see the router module docstring.
    assert run["total_reconciled_value"] == "570.00"
    assert run["total_disputed_value"] == "846.00"
    assert run["money_at_risk"] == "359.50"
    assert run["status"] == "complete"
    assert len(connection.discrepancies) == 15


def test_runs_latest_route_404_before_any_run():
    connection = _FakeConnection()
    app = _make_test_app(connection, str(uuid.uuid4()))
    client = TestClient(app)

    response = client.get("/api/reconcile/runs/latest")

    assert response.status_code == 404


def test_runs_latest_route_returns_run_and_by_type(seeded_connection):
    connection, user_id = seeded_connection
    app = _make_test_app(connection, user_id)
    client = TestClient(app)
    client.post("/api/reconcile/run")

    response = client.get("/api/reconcile/runs/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["orders_count"] == 14
    by_type = {b["type"]: b for b in body["by_type"]}
    assert by_type["MISSING_PAYMENT"] == {"type": "MISSING_PAYMENT", "count": 1, "value": "75.00"}
    assert by_type["ORPHAN_PAYMENT"]["count"] == 1


def test_discrepancies_route_404_before_any_run():
    connection = _FakeConnection()
    app = _make_test_app(connection, str(uuid.uuid4()))
    client = TestClient(app)

    response = client.get("/api/discrepancies")

    assert response.status_code == 404


def test_discrepancies_route_filters_by_type(seeded_connection):
    connection, user_id = seeded_connection
    app = _make_test_app(connection, user_id)
    client = TestClient(app)
    client.post("/api/reconcile/run")

    response = client.get("/api/discrepancies", params={"type": "UNSETTLED_PAYMENT"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {r["order_id"] for r in body["results"]} == {"FIX-1007", "FIX-1008"}


def test_discrepancies_route_paginates(seeded_connection):
    connection, user_id = seeded_connection
    app = _make_test_app(connection, user_id)
    client = TestClient(app)
    client.post("/api/reconcile/run")

    response = client.get("/api/discrepancies", params={"page": 1, "page_size": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 15
    assert body["page"] == 1
    assert body["page_size"] == 5
    assert len(body["results"]) == 5


def test_reconcile_routes_require_auth():
    app = FastAPI()
    app.include_router(reconcile_router.router, prefix="/api")
    # Only the settings sub-dependency is overridden (so this doesn't need a
    # real .env) -- get_current_user_id itself must still reject a missing
    # Authorization header with 401 before any handler logic runs.
    app.dependency_overrides[get_settings] = lambda: object()
    client = TestClient(app)

    assert client.post("/api/reconcile/run").status_code == 401
    assert client.get("/api/reconcile/runs/latest").status_code == 401
    assert client.get("/api/discrepancies").status_code == 401
