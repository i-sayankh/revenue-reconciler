"""asyncpg connection pool for the app.

The pool is created once on FastAPI startup (see the lifespan handler in
main.py) and closed on shutdown. Route handlers reach it through the
`get_connection` dependency, which acquires and releases a pooled
connection per request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg

from app.config import Settings

_pool: asyncpg.Pool | None = None


async def connect_db(settings: Settings | None = None) -> asyncpg.Pool:
    """Create the connection pool if it doesn't exist yet, and return it."""
    global _pool
    if _pool is None:
        settings = settings or Settings()
        _pool = await asyncpg.create_pool(settings.database_url)
    return _pool


async def close_db() -> None:
    """Close the connection pool, if one is open."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Return the active pool.

    Raises RuntimeError if called before `connect_db()` has run (e.g. the
    app hasn't started up yet).
    """
    if _pool is None:
        raise RuntimeError(
            "Database pool is not initialized -- connect_db() has not run yet."
        )
    return _pool


async def get_connection() -> AsyncIterator[asyncpg.Connection]:
    """FastAPI dependency: acquire a pooled connection for the request."""
    pool = get_pool()
    async with pool.acquire() as connection:
        yield connection
