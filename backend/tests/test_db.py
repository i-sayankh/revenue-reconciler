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

    async def fake_create_pool(dsn):
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

    async def fake_create_pool(dsn):
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

    async def fake_create_pool(dsn):
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
