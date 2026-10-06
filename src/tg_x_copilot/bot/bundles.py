"""Opt-in short-burst grouping, isolated per operator/chat. Shutdown flushes to durable intake."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..image_settings import ImagePreferences

log = logging.getLogger(__name__)
Emit = Callable[[list[Any], ImagePreferences], Awaitable[None]]


@dataclass
class PendingBundle:
    preferences: ImagePreferences
    emit: Emit
    started: float
    messages: dict[int, Any] = field(default_factory=dict)
    timer: asyncio.Task | None = None


class BundleCollector:
    def __init__(
        self, *, idle_seconds: float = 3, max_age_seconds: float = 12, max_messages: int = 20
    ):
        self.idle_seconds = idle_seconds
        self.max_age_seconds = max_age_seconds
        self.max_messages = max_messages
        self.pending: dict[tuple[int, int], PendingBundle] = {}
        self.timers: set[asyncio.Task] = set()

    async def add(
        self, key: tuple[int, int], messages: list[Any], preferences: ImagePreferences, emit: Emit
    ) -> None:
        now = asyncio.get_running_loop().time()
        entry = self.pending.get(key)
        if entry is None:
            entry = PendingBundle(preferences.model_copy(deep=True), emit, now)
            self.pending[key] = entry
        for message in messages:
            if message is not None:
                entry.messages[message.id] = message
        if len(entry.messages) >= self.max_messages or now - entry.started >= self.max_age_seconds:
            await self.flush(key)
            return
        if entry.timer:
            entry.timer.cancel()
        delay = min(self.idle_seconds, self.max_age_seconds - (now - entry.started))
        entry.timer = asyncio.create_task(self._later(key, delay), name="auto-bundle-intake")
        self.timers.add(entry.timer)
        entry.timer.add_done_callback(self.timers.discard)

    async def _later(self, key, delay):
        try:
            await asyncio.sleep(delay)
            await self.flush(key)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("automatic bundle intake failed")

    async def flush(self, key) -> None:
        entry = self.pending.pop(key, None)
        if entry is None:
            return
        if entry.timer and entry.timer is not asyncio.current_task():
            entry.timer.cancel()
            await asyncio.gather(entry.timer, return_exceptions=True)
        if entry.messages:
            await entry.emit(
                [entry.messages[idx] for idx in sorted(entry.messages)], entry.preferences
            )

    async def close(self) -> None:
        # Pending buffers are persisted before worker shutdown. Timers already emitting are awaited.
        for key in list(self.pending):
            await self.flush(key)
        active = [task for task in self.timers if task is not asyncio.current_task()]
        if active:
            await asyncio.gather(*active, return_exceptions=True)
