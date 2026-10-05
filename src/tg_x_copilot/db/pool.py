"""PyMySQL access that never blocks the event loop and never shares a connection across threads.

Threading model (simplest safe MVP): every DB operation runs in one `asyncio.to_thread` call
that opens its own connection, uses it, and closes it, all in that same worker thread. A PyMySQL
connection is not thread-safe, and `to_thread` may run consecutive calls on different executor
threads, so connections are never pooled or handed between threads. Concurrency is bounded by
the `db` semaphore. The per-operation connect cost (~1-3 ms on localhost) is fine at MVP volume;
a thread-confined pool can replace this later without changing the async API.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, TypeVar

import pymysql
import pymysql.cursors

from ..config import DBSettings

log = logging.getLogger(__name__)
T = TypeVar("T")


class Database:
    def __init__(self, cfg: DBSettings, semaphore: asyncio.Semaphore) -> None:
        self._cfg = cfg
        self._sem = semaphore
        self._closed = False

    # ------------------------------------------------------------ sync internals (thread)

    def _connect(self) -> pymysql.connections.Connection:
        c = self._cfg
        return pymysql.connect(
            host=c.host,
            port=c.port,
            user=c.user,
            password=c.password.get_secret_value(),
            database=c.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=c.connect_timeout,
            read_timeout=c.read_timeout,
            write_timeout=c.read_timeout,
        )

    def _run(self, fn: Callable[[pymysql.connections.Connection], T]) -> T:
        """Runs entirely inside one worker thread: connect -> fn -> close."""
        conn = self._connect()
        try:
            return fn(conn)
        finally:
            _safe_close(conn)

    # ------------------------------------------------------------ async API

    async def run(self, fn: Callable[[pymysql.connections.Connection], T]) -> T:
        if self._closed:
            raise RuntimeError("database is closed")
        async with self._sem:
            return await asyncio.to_thread(self._run, fn)

    async def fetchone(self, sql: str, args: Any = None) -> dict[str, Any] | None:
        def op(conn: pymysql.connections.Connection) -> dict[str, Any] | None:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchone()

        return await self.run(op)

    async def fetchall(self, sql: str, args: Any = None) -> list[dict[str, Any]]:
        def op(conn: pymysql.connections.Connection) -> list[dict[str, Any]]:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return list(cur.fetchall())

        return await self.run(op)

    async def execute(self, sql: str, args: Any = None) -> int:
        """Returns affected row count."""

        def op(conn: pymysql.connections.Connection) -> int:
            with conn.cursor() as cur:
                return cur.execute(sql, args)

        return await self.run(op)

    async def executemany(self, sql: str, rows: list[Any]) -> int:
        if not rows:
            return 0

        def op(conn: pymysql.connections.Connection) -> int:
            conn.begin()
            try:
                with conn.cursor() as cur:
                    n = cur.executemany(sql, rows)
                conn.commit()
                return n or 0
            except Exception:
                conn.rollback()
                raise

        return await self.run(op)

    async def ping(self) -> None:
        await self.fetchone("SELECT 1 AS ok")

    async def close(self) -> None:
        self._closed = True  # no pooled connections to drain
        log.info("database closed")


def _safe_close(conn: pymysql.connections.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass
