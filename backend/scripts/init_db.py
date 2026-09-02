"""One-shot script: apply backend/schema.sql to the database at DATABASE_URL.

Usage (from the backend/ directory, with the venv active and a real
DATABASE_URL set in the environment or in backend/.env):

    python scripts/init_db.py

The schema is idempotent (every statement is `create ... if not exists`),
so running this against an already-initialized database is a no-op.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import asyncpg

# Allow `from app.config import Settings` whether this script is run as
# `python scripts/init_db.py` from backend/, or invoked from elsewhere.
_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.config import Settings  # noqa: E402

SCHEMA_PATH = _BACKEND_DIR / "schema.sql"


def read_schema(schema_path: pathlib.Path = SCHEMA_PATH) -> str:
    """Read the schema file verbatim."""
    return schema_path.read_text(encoding="utf-8")


def get_database_url() -> str:
    """Read DATABASE_URL via the app's Settings (backend/.env or environment)."""
    return Settings().database_url


async def run(database_url: str, schema_sql: str | None = None) -> None:
    """Connect to `database_url` and execute the schema against it."""
    schema_sql = schema_sql if schema_sql is not None else read_schema()
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(schema_sql)
    finally:
        await conn.close()


def main() -> None:
    database_url = get_database_url()
    asyncio.run(run(database_url))
    print("schema.sql applied successfully.")


if __name__ == "__main__":
    main()
