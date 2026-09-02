"""GET /api/whoami -- proves the auth dependency works end to end.

This route has no purpose beyond exercising `get_current_user_id`; later
steps protect the real ingest/reconcile/discrepancies routes with the same
dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import get_current_user_id

router = APIRouter()


@router.get("/whoami")
def whoami(user_id: str = Depends(get_current_user_id)) -> dict[str, str]:
    return {"user_id": user_id}
