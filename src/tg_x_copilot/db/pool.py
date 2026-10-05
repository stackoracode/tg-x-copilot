"""Tiny PyMySQL connection pool that never blocks the event loop.

Every DB call runs in a worker thread via `asyncio.to_thread`, gated by the `db` semaphore,
so at most `concurrency.db` threads talk to MySQL at once and the pool never grows beyond that.
"""

from __future__ import annotations

import asyncio
import logging
import queue
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
        self._idle: queue.LifoQueue[pymysql.connections.Connection] = queue.LifoQueue()
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

    def _acquire(self) -> pymysql.connections.Connection:
        try:
            conn = self._idle.get_nowait()
        except queue.Empty:
            return self._connect()
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            _safe_close(conn)
            return self._connect()

    def _release(self, conn: pymysql.connections.Connection) -> None:
        if self._closed or self._idle.qsize() >= self._cfg.pool_size:
            _safe_close(conn)
        else:
            self._idle.put(conn)

    def _run(self, fn: Callable[[pymysql.connections.Connection], T]) -> T:
        conn = self._acquire()
        try:
            result = fn(conn)
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
            _safe_close(conn)  # connection may be broken; don't return it to the pool
            raise
        except Exception:
            self._release(conn)
            raise
        self._release(conn)
        return result

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
        self._closed = True

        def drain() -> None:
            while True:
                try:
                    _safe_close(self._idle.get_nowait())
                except queue.Empty:
                    return

        await asyncio.to_thread(drain)
        log.info("database pool closed")


def _safe_close(conn: pymysql.connections.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass
