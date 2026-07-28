"""Manage the API's asynchronous read-only Postgres connection pool."""

from __future__ import annotations

import asyncpg


class DatabaseUnavailableError(RuntimeError):
    """Signal that the API cannot currently query Postgres."""


class Database:
    """Own the API's bounded asyncpg connection pool."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise DatabaseUnavailableError("database pool is not initialized")
        return self._pool

    async def connect(self) -> None:
        if self._pool is not None:
            return
        try:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)
        except (asyncpg.PostgresError, OSError):
            # Keep the HTTP process alive so readiness can report the dependency failure.
            self._pool = None

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ready(self) -> bool:
        if self._pool is None:
            await self.connect()
        try:
            return await self.pool.fetchval("select 1") == 1
        except (RuntimeError, asyncpg.PostgresError, OSError):
            return False
