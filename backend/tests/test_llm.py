"""Tests for backend/app/llm/groq_client.py and the
`POST /api/discrepancies/{id}/explain` route in backend/app/routers/reconcile.py.

`explain_discrepancy` is exercised against a fake Groq client -- a small
stand-in exposing the same `client.chat.completions.create(...)` shape the
real `groq.AsyncGroq` client has, returning canned `choices[0].message.content`
strings -- so the Groq SDK call itself is mocked rather than `httpx` at a
lower level, per the brief's mocking guidance. This proves the
validate/retry/fallback logic without any network access or a real
`GROQ_API_KEY`.

The route is exercised with FastAPI's TestClient against a minimal app that
only mounts the reconcile router, with `get_current_user_id`/`get_connection`
overridden (same pattern as test_reconcile.py) and `explain_discrepancy`
monkeypatched at its import site in the router module -- proving auth
scoping, the cache-hit short-circuit, and persistence of a freshly-generated
explanation, all without a live database or a real Groq call.

The live acceptance check (one real manual call against the real Groq API
for one real discrepancy) is separate and out of scope for this file -- see
the task report for that proof.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import get_current_user_id
from app.db import get_connection
from app.llm.groq_client import FALLBACK_EXPLANATION, Explanation, explain_discrepancy
from app.reconcile import service
from app.routers import reconcile as reconcile_router

VALID_JSON_RESPONSE = (
    '{"summary": "Order was completed but no matching charge was found.", '
    '"likely_cause": "The payment processor charge may have failed silently or '
    'was never initiated.", '
    '"recommended_action": "Check the payment processor dashboard for a failed '
    'or missing charge attempt and re-bill the customer if needed.", '
    '"confidence": "medium"}'
)

MALFORMED_RESPONSE = "Sure, here's an explanation: the order and payment don't match up."


class _FakeMessage:
    def __init__(self, content: str | None):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None):
        self.message = _FakeMessage(content)


class _FakeCompletionResponse:
    def __init__(self, content: str | None):
        self.choices = [_FakeChoice(content)]


class _FakeGroqClient:
    """Stands in for `groq.AsyncGroq`: `client.chat.completions.create(...)`
    returns the next canned response string (as `choices[0].message.content`)
    from `responses`, in order. Records every call's `messages` for
    inspection."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> _FakeCompletionResponse:
        self.calls.append(kwargs)
        content = self._responses.pop(0)
        return _FakeCompletionResponse(content)


SAMPLE_DISCREPANCY = {
    "type": "MISSING_PAYMENT",
    "order_id": "ORD-1042",
    "payment_ref": None,
    "order_amount": Decimal("75.00"),
    "payment_amount": None,
    "currency_order": "USD",
    "currency_payment": None,
    "difference": None,
    "detail": {"reason": "order is completed but no charge payment was matched"},
}


# -- explain_discrepancy: valid response -------------------------------------


async def test_explain_discrepancy_returns_validated_explanation_on_valid_response():
    client = _FakeGroqClient([VALID_JSON_RESPONSE])

    result = await explain_discrepancy(SAMPLE_DISCREPANCY, client=client)

    assert isinstance(result, Explanation)
    assert result.confidence == "medium"
    assert "charge" in result.likely_cause.lower()
    assert len(client.calls) == 1  # no retry needed
    # Temperature and model are pinned per the brief -- prove they're sent.
    assert client.calls[0]["temperature"] == 0.2
    assert client.calls[0]["model"] == "openai/gpt-oss-120b"
    # The prompt carries the discrepancy's fixed fields, not a re-derivation.
    user_message = client.calls[0]["messages"][1]["content"]
    assert "MISSING_PAYMENT" in user_message
    assert "ORD-1042" in user_message


async def test_explain_discrepancy_retries_once_then_succeeds_on_malformed_first_reply():
    client = _FakeGroqClient([MALFORMED_RESPONSE, VALID_JSON_RESPONSE])

    result = await explain_discrepancy(SAMPLE_DISCREPANCY, client=client)

    assert isinstance(result, Explanation)
    assert result.confidence == "medium"
    assert len(client.calls) == 2
    # The retry prompt adds the stricter "JSON only" instruction.
    retry_message = client.calls[1]["messages"][1]["content"]
    assert "no markdown fences" in retry_message.lower()


# -- explain_discrepancy: malformed response on both tries -> fallback ------


async def test_explain_discrepancy_falls_back_after_two_malformed_responses():
    client = _FakeGroqClient([MALFORMED_RESPONSE, MALFORMED_RESPONSE])

    result = await explain_discrepancy(SAMPLE_DISCREPANCY, client=client)

    assert result == FALLBACK_EXPLANATION
    assert result.confidence == "low"
    assert len(client.calls) == 2  # one retry, then give up -- no exception raised


async def test_explain_discrepancy_falls_back_on_schema_mismatch_both_tries():
    """Valid JSON, but missing/wrong-typed required fields -- still a
    validation failure, not a parse failure, and must still fall back."""
    bad_schema = '{"summary": "ok", "confidence": "extremely-confident"}'
    client = _FakeGroqClient([bad_schema, bad_schema])

    result = await explain_discrepancy(SAMPLE_DISCREPANCY, client=client)

    assert result == FALLBACK_EXPLANATION
    assert len(client.calls) == 2


async def test_explain_discrepancy_falls_back_when_client_raises_both_tries():
    """A call-time failure (network/API error) must not propagate as a 500
    either -- same fallback path as a malformed response."""

    class _RaisingClient:
        def __init__(self):
            self.calls = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **kwargs):
            self.calls += 1
            raise RuntimeError("simulated Groq API failure")

    client = _RaisingClient()

    result = await explain_discrepancy(SAMPLE_DISCREPANCY, client=client)

    assert result == FALLBACK_EXPLANATION
    assert client.calls == 2


# -- POST /api/discrepancies/{id}/explain route ------------------------------


class _FakeConnection:
    """Minimal in-memory stand-in for `asyncpg.Connection`, recognizing only
    the two query shapes `fetch_discrepancy_for_user`/
    `persist_discrepancy_explanation` issue."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.executed: list[tuple[str, tuple]] = []

    @staticmethod
    def _norm(sql: str) -> str:
        return " ".join(sql.split()).lower()

    async def fetchrow(self, sql, *params):
        norm = self._norm(sql)
        if norm.startswith("select * from discrepancies where id"):
            discrepancy_id, user_id = params
            for row in self.rows:
                if row["id"] == discrepancy_id and row["user_id"] == user_id:
                    return row
            return None
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql, *params):
        norm = self._norm(sql)
        if norm.startswith("update discrepancies set explanation"):
            explanation, explained_at, discrepancy_id = params
            for row in self.rows:
                if row["id"] == discrepancy_id:
                    row["explanation"] = explanation
                    row["explained_at"] = explained_at
            self.executed.append((sql, params))
            return
        raise AssertionError(f"unexpected execute SQL: {sql}")


def _discrepancy_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "type": "MISSING_PAYMENT",
        "order_id": "ORD-1042",
        "payment_ref": None,
        "order_amount": Decimal("75.00"),
        "payment_amount": None,
        "currency_order": "USD",
        "currency_payment": None,
        "difference": None,
        "detail": {"reason": "order is completed but no charge payment was matched"},
        "explanation": None,
        "explained_at": None,
    }
    row.update(overrides)
    return row


def _make_test_app(connection: _FakeConnection, user_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(reconcile_router.router, prefix="/api")
    app.dependency_overrides[get_current_user_id] = lambda: user_id

    async def _fake_get_connection():
        yield connection

    app.dependency_overrides[get_connection] = _fake_get_connection
    return app


def test_explain_route_404_when_discrepancy_belongs_to_another_user():
    other_user = uuid.uuid4()
    row = _discrepancy_row(user_id=other_user)
    connection = _FakeConnection([row])
    app = _make_test_app(connection, str(uuid.uuid4()))
    client = TestClient(app)

    response = client.post(f"/api/discrepancies/{row['id']}/explain")

    assert response.status_code == 404


def test_explain_route_404_when_discrepancy_does_not_exist():
    connection = _FakeConnection([])
    app = _make_test_app(connection, str(uuid.uuid4()))
    client = TestClient(app)

    response = client.post(f"/api/discrepancies/{uuid.uuid4()}/explain")

    assert response.status_code == 404


def test_explain_route_returns_cached_explanation_without_calling_groq(monkeypatch):
    user_id = uuid.uuid4()
    cached = {
        "summary": "cached summary",
        "likely_cause": "cached cause",
        "recommended_action": "cached action",
        "confidence": "high",
    }
    row = _discrepancy_row(user_id=user_id, explanation=cached, explained_at=datetime.now(timezone.utc))
    connection = _FakeConnection([row])
    app = _make_test_app(connection, str(user_id))
    client = TestClient(app)

    called = False

    async def _should_not_be_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Groq must not be called for an already-cached explanation")

    monkeypatch.setattr(reconcile_router, "explain_discrepancy", _should_not_be_called)

    response = client.post(f"/api/discrepancies/{row['id']}/explain")

    assert response.status_code == 200
    assert response.json()["explanation"] == cached
    assert called is False


def test_explain_route_generates_and_persists_when_not_cached(monkeypatch):
    user_id = uuid.uuid4()
    row = _discrepancy_row(user_id=user_id)
    connection = _FakeConnection([row])
    app = _make_test_app(connection, str(user_id))
    client = TestClient(app)

    generated = Explanation(
        summary="generated summary",
        likely_cause="generated cause",
        recommended_action="generated action",
        confidence="low",
    )

    async def _fake_explain(discrepancy, **kwargs):
        assert discrepancy["order_id"] == "ORD-1042"
        return generated

    monkeypatch.setattr(reconcile_router, "explain_discrepancy", _fake_explain)

    response = client.post(f"/api/discrepancies/{row['id']}/explain")

    assert response.status_code == 200
    body = response.json()
    assert body["explanation"] == generated.model_dump()
    assert body["explained_at"] is not None
    # Persisted onto the row, so a second call would hit the cache path.
    assert row["explanation"] == generated.model_dump()
    assert row["explained_at"] is not None
    assert len(connection.executed) == 1


def test_explain_route_requires_auth():
    from app.auth import get_settings

    app = FastAPI()
    app.include_router(reconcile_router.router, prefix="/api")
    app.dependency_overrides[get_settings] = lambda: object()
    client = TestClient(app)

    response = client.post(f"/api/discrepancies/{uuid.uuid4()}/explain")

    assert response.status_code == 401
