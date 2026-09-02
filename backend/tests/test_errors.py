"""Tests for backend/app/errors.py -- the global JSON error-response shape.

Exercised against a fresh, router-less FastAPI() app (same pattern as
test_ingest.py/test_reconcile.py) with a couple of throwaway routes that
deliberately raise each kind of error `register_exception_handlers` covers,
so the handler logic is proven without needing the real app (whose
lifespan opens a live DB pool -- see test_main.py for that app wired up
end to end).
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.testclient import TestClient

from app.errors import register_exception_handlers


def _make_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom-http")
    def boom_http():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thing not found")

    @app.get("/boom-validation")
    def boom_validation(count: int = Query(...)):
        return {"count": count}

    @app.get("/boom-unhandled")
    def boom_unhandled():
        raise RuntimeError("kaboom")

    return app


def test_http_exception_returns_error_envelope_and_keeps_status_code():
    client = TestClient(_make_app())

    response = client.get("/boom-http")

    assert response.status_code == 404
    assert response.json() == {"error": {"message": "thing not found", "code": "not_found"}}


def test_validation_error_returns_error_envelope_with_field_details():
    client = TestClient(_make_app())

    response = client.get("/boom-validation", params={"count": "not-a-number"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"] == "request validation failed"
    assert body["error"]["details"][0]["field"] == "count"


def test_unmatched_route_returns_error_envelope():
    client = TestClient(_make_app())

    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_unhandled_exception_returns_500_error_envelope():
    client = TestClient(_make_app(), raise_server_exceptions=False)

    response = client.get("/boom-unhandled")

    assert response.status_code == 500
    assert response.json() == {"error": {"message": "internal server error", "code": "internal_error"}}
