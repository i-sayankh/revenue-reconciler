"""Tests for backend/app/main.py -- CORS and global error-handler wiring on
the real app object (not a throwaway router-only app like the other route
test files use).

Only routes that don't touch the DB are exercised (`/health`, and
`/api/whoami`'s auth-only 401 path), so `TestClient(app)` is used *without*
the `with` context-manager form -- Starlette only runs the lifespan
(`connect_db()`, which needs a real Postgres pool) when the client is
entered as a context manager, so a plain `TestClient(app)` here never
touches the database.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app, settings


def test_cors_preflight_allows_configured_origin():
    client = TestClient(app)
    origin = settings.allowed_origins_list[0]

    response = client.options(
        "/api/whoami",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_cors_preflight_rejects_unconfigured_origin():
    client = TestClient(app)

    response = client.options(
        "/api/whoami",
        headers={"Origin": "https://not-allowed.example.com", "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_health_route_still_works():
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unauthenticated_request_returns_error_envelope():
    client = TestClient(app)

    response = client.get("/api/whoami")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
