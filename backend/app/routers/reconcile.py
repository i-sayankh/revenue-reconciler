"""POST /api/reconcile/run, GET /api/reconcile/runs/latest, GET /api/discrepancies.

Wires the deterministic engine (`app.engine.reconcile`) and its DB-facing
helpers (`app.reconcile.service`) together behind the Step 4 auth
dependency. All three routes are scoped by the verified `user_id`.

Response shapes (documented here since Step 13's frontend consumes these
directly -- see each route's docstring for the exact fields):

- `POST /api/reconcile/run` -> `{"run": {...headline fields...}}`
- `GET /api/reconcile/runs/latest` -> `{"run": {...}, "by_type": [...]}`
- `GET /api/discrepancies` -> `{"total", "page", "page_size", "results": [...]}`

Decimal fields (`total_reconciled_value`, `total_disputed_value`,
`money_at_risk`, `value`, `order_amount`, `payment_amount`, `difference`)
serialize as **JSON strings** (e.g. `"570.00"`), not JSON numbers -- this is
FastAPI/pydantic v2's return-type-based response serialization keeping full
decimal precision instead of a lossy float conversion. The frontend (Step
13) must parse these with a numeric conversion (e.g. `Number(...)` or a
decimal library) rather than assume a raw JSON number.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import get_current_user_id
from app.db import get_connection
from app.models import Discrepancy, ReconciliationRun
from app.reconcile import service

router = APIRouter()

_NO_UPLOADS_DETAIL = (
    "no uploaded orders or payments found for this user -- upload both files before running reconciliation"
)
_NO_RUN_DETAIL = "no reconciliation run found for this user -- run POST /api/reconcile/run first"


def _run_to_dict(run: ReconciliationRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "created_at": run.created_at,
        "orders_count": run.orders_count,
        "payments_count": run.payments_count,
        "total_reconciled_value": run.total_reconciled_value,
        "total_disputed_value": run.total_disputed_value,
        "money_at_risk": run.money_at_risk,
        "status": run.status,
    }


def _discrepancy_to_dict(d: Discrepancy) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "type": d.type,
        "order_id": d.order_id,
        "payment_ref": d.payment_ref,
        "order_amount": d.order_amount,
        "payment_amount": d.payment_amount,
        "currency_order": d.currency_order,
        "currency_payment": d.currency_payment,
        "difference": d.difference,
        "detail": d.detail,
        "explanation": d.explanation,
        "explained_at": d.explained_at,
    }


@router.post("/reconcile/run")
async def run_reconciliation(
    user_id: str = Depends(get_current_user_id),
    connection: asyncpg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Run the engine over the caller's latest uploaded orders+payments and
    persist a run + its discrepancies.

    "Latest uploaded orders+payments" means the most recent
    `upload_batch_id` in `orders` for this user and, independently, the
    most recent `upload_batch_id` in `payments` for this user (they are
    uploaded via two separate calls and generally will not share a batch).

    404s are not used here: if the user has never uploaded *anything* at
    all (`orders` and `payments` both empty for them), this returns
    `400 Bad Request` rather than silently persisting an empty run or
    raising a 500.

    Response: `{"run": {"id", "created_at", "orders_count", "payments_count",
    "total_reconciled_value", "total_disputed_value", "money_at_risk",
    "status"}}`.
    """
    orders = await service.fetch_latest_orders(connection, user_id)
    payments = await service.fetch_latest_payments(connection, user_id)

    if not orders and not payments:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_NO_UPLOADS_DETAIL)

    result = service.run_reconciliation(orders, payments)
    run = await service.persist_run(
        connection,
        user_id=user_id,
        result=result,
        orders_count=len(orders),
        payments_count=len(payments),
    )
    return {"run": _run_to_dict(run)}


@router.get("/reconcile/runs/latest")
async def get_latest_run(
    user_id: str = Depends(get_current_user_id),
    connection: asyncpg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Headline metrics for the caller's most recent reconciliation run,
    plus a per-type count/value breakdown -- chart-ready for the frontend's
    stat cards and bar chart (Step 13).

    `404` if the user has never run a reconciliation.

    Response:
        {
          "run": {"id", "created_at", "orders_count", "payments_count",
                   "total_reconciled_value", "total_disputed_value",
                   "money_at_risk", "status"},
          "by_type": [{"type": "MISSING_PAYMENT", "count": 1, "value": 75.00}, ...]
        }

    `by_type` only lists types with at least one discrepancy row in the run
    (no zero-count entries) and is ordered alphabetically by type. Its
    `value` is that type's total `order_amount` (falling back to
    `payment_amount` for ORPHAN_PAYMENT rows, which have no order). The
    RECONCILED bucket's `value` deliberately excludes the engine's
    `NO_CHARGE_ACTIVITY` fallback rows (its `count` still includes them) so
    it always matches `run.total_reconciled_value` -- see
    `app.reconcile.service.fetch_by_type_summary`'s docstring.
    """
    run = await service.fetch_latest_run(connection, user_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_RUN_DETAIL)
    by_type = await service.fetch_by_type_summary(connection, run.id)
    return {"run": _run_to_dict(run), "by_type": by_type}


@router.get("/discrepancies")
async def list_discrepancies(
    type: str | None = Query(default=None, description="exact match on discrepancy type, e.g. MISSING_PAYMENT"),
    q: str | None = Query(default=None, description="case-insensitive substring match on order_id or payment_ref"),
    min: Decimal | None = Query(default=None, description="minimum order_amount, inclusive"),
    max: Decimal | None = Query(default=None, description="maximum order_amount, inclusive"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
    connection: asyncpg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Paginated, filterable discrepancy rows for the caller's *latest run
    only* -- there is no `run_id` filter; the dashboard is a single
    current-state view over the most recent run.

    `404` if the user has never run a reconciliation (same condition as
    `GET /api/reconcile/runs/latest`).

    Filters (all optional, AND-combined):
    - `type`: exact match on the discrepancy type.
    - `q`: case-insensitive substring match against `order_id` OR
      `payment_ref`.
    - `min`/`max`: inclusive range over `order_amount` -- chosen as the
      single most useful amount field for a human filtering "discrepancies
      between $X and $Y"; rows with no `order_amount` (ORPHAN_PAYMENT) are
      excluded whenever either bound is supplied.
    - `page`/`page_size`: standard 1-based offset pagination (defaults 1/20,
      `page_size` capped at 200).

    Response: `{"total": <matching row count, ignoring pagination>, "page",
    "page_size", "results": [{"id", "type", "order_id", "payment_ref",
    "order_amount", "payment_amount", "currency_order", "currency_payment",
    "difference", "detail", "explanation", "explained_at"}, ...]}`.
    """
    run = await service.fetch_latest_run(connection, user_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_RUN_DETAIL)

    rows, total = await service.fetch_discrepancies(
        connection,
        run_id=run.id,
        type_=type,
        q=q,
        min_amount=min,
        max_amount=max,
        page=page,
        page_size=page_size,
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "results": [_discrepancy_to_dict(r) for r in rows],
    }
