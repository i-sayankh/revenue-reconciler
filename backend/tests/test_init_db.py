"""Tests for backend/scripts/init_db.py that don't require a live database.

`asyncpg.connect` is monkeypatched with a fake connection so the script's
logic (read schema.sql, execute it, close the connection) is exercised
without a real Postgres connection.
"""

import pytest

from scripts import init_db


class _FakeConnection:
    def __init__(self):
        self.executed = []
        self.closed = False

    async def execute(self, sql):
        self.executed.append(sql)

    async def close(self):
        self.closed = True


def test_read_schema_matches_schema_sql_file():
    schema_sql = init_db.read_schema()
    on_disk = init_db.SCHEMA_PATH.read_text(encoding="utf-8")

    assert schema_sql == on_disk
    assert "create table if not exists orders" in schema_sql
    assert "create table if not exists payments" in schema_sql
    assert "create table if not exists reconciliation_runs" in schema_sql
    assert "create table if not exists discrepancies" in schema_sql
    # Idempotent: every table/index statement guards with if not exists.
    assert schema_sql.count("create table if not exists") == 4
    assert schema_sql.count("create index if not exists") == 4


def test_get_database_url_reads_from_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake-user:fake-pass@localhost/fake")
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("GROQ_API_KEY", "fake-key")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:3000")

    assert init_db.get_database_url() == "postgresql://fake-user:fake-pass@localhost/fake"


async def test_run_executes_schema_and_closes_connection(monkeypatch):
    fake_conn = _FakeConnection()

    async def fake_connect(dsn):
        assert dsn == "postgresql://fake-user:fake-pass@localhost/fake"
        return fake_conn

    monkeypatch.setattr(init_db.asyncpg, "connect", fake_connect)

    await init_db.run("postgresql://fake-user:fake-pass@localhost/fake", schema_sql="select 1;")

    assert fake_conn.executed == ["select 1;"]
    assert fake_conn.closed is True


async def test_run_closes_connection_even_if_execute_fails(monkeypatch):
    fake_conn = _FakeConnection()

    async def failing_execute(sql):
        raise RuntimeError("boom")

    fake_conn.execute = failing_execute

    async def fake_connect(dsn):
        return fake_conn

    monkeypatch.setattr(init_db.asyncpg, "connect", fake_connect)

    with pytest.raises(RuntimeError):
        await init_db.run("postgresql://fake-user:fake-pass@localhost/fake", schema_sql="select 1;")

    assert fake_conn.closed is True
