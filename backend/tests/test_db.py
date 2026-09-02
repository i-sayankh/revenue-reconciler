"""Tests for backend/app/db.py that don't require a live database.

`connect_db`/`asyncpg.create_pool` is monkeypatched with a fake pool so the
pool-lifecycle logic (create once, expose via get_pool, close and clear on
shutdown) is exercised without a real Postgres connection.
"""

import pytest

from app import db as db_module


class _FakeConnection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakePool:
    def __init__(self):
        self.closed = False

    def acquire(self):
        return _FakeConnection()

    async def close(self):
        self.closed = True


class _FakeCodecConnection:
    """Records `set_type_codec` calls so the init callback can be asserted on."""

    def __init__(self):
        self.registered: list[dict] = []

    async def set_type_codec(self, pg_type, *, encoder, decoder, schema):
        self.registered.append(
            {"pg_type": pg_type, "encoder": encoder, "decoder": decoder, "schema": schema}
        )


@pytest.fixture(autouse=True)
def reset_pool():
    """Ensure module-level pool state doesn't leak between tests."""
    db_module._pool = None
    yield
    db_module._pool = None


def test_get_pool_raises_before_connect():
    with pytest.raises(RuntimeError):
        db_module.get_pool()


async def test_connect_db_creates_and_reuses_pool(monkeypatch):
    fake_pool = _FakePool()
    calls = []

    async def fake_create_pool(dsn, init=None):
        calls.append(dsn)
        return fake_pool

    monkeypatch.setattr(db_module.asyncpg, "create_pool", fake_create_pool)

    class FakeSettings:
        database_url = "postgresql://fake-user:fake-pass@localhost/fake"

    pool = await db_module.connect_db(settings=FakeSettings())

    assert pool is fake_pool
    assert db_module.get_pool() is fake_pool
    assert calls == ["postgresql://fake-user:fake-pass@localhost/fake"]

    # Calling again must not create a second pool.
    pool_again = await db_module.connect_db(settings=FakeSettings())
    assert pool_again is fake_pool
    assert calls == ["postgresql://fake-user:fake-pass@localhost/fake"]


async def test_close_db_closes_and_clears_pool(monkeypatch):
    fake_pool = _FakePool()

    async def fake_create_pool(dsn, init=None):
        return fake_pool

    monkeypatch.setattr(db_module.asyncpg, "create_pool", fake_create_pool)

    class FakeSettings:
        database_url = "postgresql://fake-user:fake-pass@localhost/fake"

    await db_module.connect_db(settings=FakeSettings())
    await db_module.close_db()

    assert fake_pool.closed is True
    with pytest.raises(RuntimeError):
        db_module.get_pool()

    # Closing again when already closed is a no-op, not an error.
    await db_module.close_db()


async def test_get_connection_acquires_from_pool(monkeypatch):
    fake_pool = _FakePool()

    async def fake_create_pool(dsn, init=None):
        return fake_pool

    monkeypatch.setattr(db_module.asyncpg, "create_pool", fake_create_pool)

    class FakeSettings:
        database_url = "postgresql://fake-user:fake-pass@localhost/fake"

    await db_module.connect_db(settings=FakeSettings())

    agen = db_module.get_connection()
    connection = await agen.__anext__()
    assert isinstance(connection, _FakeConnection)
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()


async def test_connect_db_passes_init_callback_to_create_pool(monkeypatch):
    fake_pool = _FakePool()
    captured = {}

    async def fake_create_pool(dsn, init=None):
        captured["init"] = init
        return fake_pool

    monkeypatch.setattr(db_module.asyncpg, "create_pool", fake_create_pool)

    class FakeSettings:
        database_url = "postgresql://fake-user:fake-pass@localhost/fake"

    await db_module.connect_db(settings=FakeSettings())

    assert captured["init"] is db_module._init_connection


async def test_init_connection_registers_jsonb_and_json_codecs():
    """The init callback must decode jsonb/json columns to dict/list, not str.

    Without this, `discrepancies.detail`/`discrepancies.explanation` (both
    jsonb) would come back from asyncpg as raw JSON text, silently
    mismatching the `dict[str, Any] | None` type on `app.models.Discrepancy`.
    """
    fake_connection = _FakeCodecConnection()

    await db_module._init_connection(fake_connection)

    registered_types = [entry["pg_type"] for entry in fake_connection.registered]
    assert registered_types == ["jsonb", "json"]

    for entry in fake_connection.registered:
        assert entry["schema"] == "pg_catalog"
        # encoder/decoder round-trip a dict the way a real jsonb column would.
        payload = {"reason": "currency mismatch", "count": 2}
        encoded = entry["encoder"](payload)
        assert isinstance(encoded, str)
        assert entry["decoder"](encoded) == payload
