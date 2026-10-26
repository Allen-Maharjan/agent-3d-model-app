"""SQLite connection management, numbered migrations and explicit transactions."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import aiosqlite

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Database:
    """A single shared connection; transactions are serialised by a lock.

    The application runs one server worker, so one connection with an
    in-process lock gives fully serialised, explicit transactions.
    """

    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = FULL")
        await conn.execute("PRAGMA busy_timeout = 5000")
        self._conn = conn
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not open")
        return self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    async def fetchone(self, sql: str, params: tuple[object, ...] = ()) -> aiosqlite.Row | None:
        async with self._lock:
            async with self.conn.execute(sql, params) as cur:
                return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            async with self.conn.execute(sql, params) as cur:
                return list(await cur.fetchall())

    async def _migrate(self) -> None:
        conn = self.conn
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        async with conn.execute("SELECT version FROM schema_migrations") as cur:
            applied = {row[0] for row in await cur.fetchall()}
        for version, sql in _load_migrations():
            if version in applied:
                continue
            await conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _split_sql(sql):
                    await conn.execute(statement)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (version, utcnow())
                )
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            await conn.execute("COMMIT")


def _load_migrations() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for entry in resources.files("sculpt_workspace.migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migrations must be numbered consecutively from 0001, found {versions}")
    return found


def _split_sql(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]
