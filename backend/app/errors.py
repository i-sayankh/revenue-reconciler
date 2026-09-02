"""Global exception handlers giving every route the same JSON error shape.

Without these, FastAPI/Starlette's built-in handling returns different
shapes depending on what went wrong: `{"detail": "..."}` for
`HTTPException` (auth failures, not-found, bad-request routes) and
`{"detail": [...]}` (a list of pydantic error dicts) for request
validation errors, plus an opaque unhandled traceback for anything else.

These handlers normalize all three cases to one shape:

    {"error": {"message": "<human readable message>", "code": "<snake_case code>"}}

`code` is a short machine-readable string (`"unauthorized"`, `"not_found"`,
`"validation_error"`, `"internal_error"`, ...) the frontend can switch on
without parsing the message text. Validation errors additionally carry a
`details` list (one entry per invalid field) under `error`.

Status codes are untouched by any of this -- a route that already raises
`HTTPException(404, ...)` still responds `404`; this module only reshapes
the response *body*.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, status
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_STATUS_CODE_TO_ERROR_CODE = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "validation_error",
}
_DEFAULT_ERROR_CODE = "http_error"


def _error_response(status_code: int, message: str, code: str, *, details: list | None = None) -> JSONResponse:
    error: dict = {"message": message, "code": code}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error})


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Handles `fastapi.HTTPException`/`starlette.exceptions.HTTPException`.

    Covers explicit `raise HTTPException(...)` calls (auth 401s, 400/404s
    raised by the ingest/reconcile routes) as well as Starlette's own
    routing errors (404 for an unmatched path, 405 for a wrong method).
    """
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    code = _STATUS_CODE_TO_ERROR_CODE.get(exc.status_code, _DEFAULT_ERROR_CODE)
    return _error_response(exc.status_code, message, code)


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Handles request validation failures (bad query params, malformed
    body, etc.) -- FastAPI's default `{"detail": [...]}` shape becomes the
    same `{"error": {...}}` envelope, with the per-field errors preserved
    under `details`.
    """
    _loc_prefixes = {"body", "query", "path", "header", "cookie"}
    details = [
        {
            "field": ".".join(str(part) for part in e["loc"] if part not in _loc_prefixes),
            "message": e["msg"],
        }
        for e in exc.errors()
    ]
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "request validation failed",
        "validation_error",
        details=details,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for anything not already an `HTTPException` -- logs the
    full traceback server-side and returns a generic 500 body so internal
    error details never leak to the client.
    """
    logger.exception("Unhandled exception while processing %s %s", request.method, request.url.path)
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "internal server error", "internal_error"
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire all three handlers onto `app`. Call once at app construction."""
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
