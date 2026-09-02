"""POST /api/ingest/orders and POST /api/ingest/payments.

Multipart CSV upload -> `{rows_loaded, rows_rejected, rejections}`, scoped
by the verified `user_id` from the Step 4 auth dependency. All the actual
parse/validate/reject and DB-insert logic lives in `app.ingest.loader`
(unit-tested without a live database); these routes just wire the upload
bytes, the user id, and a fresh `upload_batch_id` together.

This endpoint only loads rows -- it does not run reconciliation. Step 7's
run/reconcile endpoints consume the latest uploaded batch.
"""

from __future__ import annotations

import io
import uuid

import asyncpg
from fastapi import APIRouter, Depends, File, UploadFile

from app.auth import get_current_user_id
from app.db import get_connection
from app.ingest.loader import insert_orders, insert_payments, load_orders, load_payments

router = APIRouter()


def _to_text_lines(content: bytes) -> io.StringIO:
    return io.StringIO(content.decode("utf-8"))


@router.post("/ingest/orders")
async def ingest_orders(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
    connection: asyncpg.Connection = Depends(get_connection),
) -> dict:
    content = await file.read()
    result = load_orders(_to_text_lines(content))
    upload_batch_id = uuid.uuid4()
    await insert_orders(connection, result.rows, user_id=user_id, upload_batch_id=upload_batch_id)
    return {
        "rows_loaded": result.rows_loaded,
        "rows_rejected": result.rows_rejected,
        "rejections": [r.as_dict() for r in result.rejections],
    }


@router.post("/ingest/payments")
async def ingest_payments(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
    connection: asyncpg.Connection = Depends(get_connection),
) -> dict:
    content = await file.read()
    result = load_payments(_to_text_lines(content))
    upload_batch_id = uuid.uuid4()
    await insert_payments(connection, result.rows, user_id=user_id, upload_batch_id=upload_batch_id)
    return {
        "rows_loaded": result.rows_loaded,
        "rows_rejected": result.rows_rejected,
        "rejections": [r.as_dict() for r in result.rejections],
    }
