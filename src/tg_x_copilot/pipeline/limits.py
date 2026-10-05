"""Per-workload concurrency limits. One semaphore per kind of upstream so a slow image model
cannot starve cheap Jev scoring, and MySQL threads stay bounded."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..config import ConcurrencySettings


@dataclass(frozen=True)
class Limits:
    jev: asyncio.Semaphore
    text: asyncio.Semaphore
    vision: asyncio.Semaphore
    image: asyncio.Semaphore
    db: asyncio.Semaphore
    io: asyncio.Semaphore

    @classmethod
    def from_settings(cls, c: ConcurrencySettings) -> "Limits":
        return cls(
            jev=asyncio.Semaphore(c.jev),
            text=asyncio.Semaphore(c.text),
            vision=asyncio.Semaphore(c.vision),
            image=asyncio.Semaphore(c.image),
            db=asyncio.Semaphore(c.db),
            io=asyncio.Semaphore(c.io),
        )
