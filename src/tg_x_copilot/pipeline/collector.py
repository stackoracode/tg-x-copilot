"""Merge a burst of forwarded messages into one task.

Forwarding several Telegram posts at once arrives as separate updates (and albums as an
`events.Album`). We buffer per chat for `merge_window_seconds` after the last message and then
flush the whole batch as one group. Each new message restarts the timer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..logging_setup import ctx

log = logging.getLogger(__name__)

FlushFn = Callable[[int, int, list[Any]], Awaitable[None]]  # chat_id, user_id, messages


class MessageCollector:
    def __init__(self, flush: FlushFn, window: Callable[[], float], max_batch: int = 30) -> None:
        self._flush = flush
        self._window = window  # callable so runtime setting changes apply immediately
        self._max_batch = max_batch
        self._buffers: dict[int, tuple[int, list[Any]]] = {}
        self._timers: dict[int, asyncio.Task[None]] = {}
        self._flushing: set[asyncio.Task[None]] = set()
        self._closed = False

    def add(self, chat_id: int, user_id: int, messages: list[Any]) -> None:
        _, buf = self._buffers.setdefault(chat_id, (user_id, []))
        buf.extend(messages)
        timer = self._timers.pop(chat_id, None)
        if timer:
            timer.cancel()
        if self._closed or len(buf) >= self._max_batch:
            self._spawn_flush(chat_id)
        else:
            self._timers[chat_id] = asyncio.create_task(self._delayed(chat_id))

    async def _delayed(self, chat_id: int) -> None:
        await asyncio.sleep(self._window())
        self._timers.pop(chat_id, None)
        self._spawn_flush(chat_id)

    def _spawn_flush(self, chat_id: int) -> None:
        entry = self._buffers.pop(chat_id, None)
        if not entry:
            return
        user_id, messages = entry
        task = asyncio.create_task(self._safe_flush(chat_id, user_id, messages))
        self._flushing.add(task)
        task.add_done_callback(self._flushing.discard)

    async def _safe_flush(self, chat_id: int, user_id: int, messages: list[Any]) -> None:
        try:
            await self._flush(chat_id, user_id, messages)
        except Exception:
            log.exception("collector flush failed", extra=ctx(chat_id=chat_id, n=len(messages)))

    async def close(self) -> None:
        """Flush everything immediately (graceful shutdown). Idempotent; after the first call,
        new messages are flushed without waiting for the merge window."""
        self._closed = True
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        for chat_id in list(self._buffers):
            self._spawn_flush(chat_id)
        if self._flushing:
            await asyncio.gather(*self._flushing, return_exceptions=True)
