"""Per-operator non-secret image preferences, persisted separately from runtime config."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..image_settings import ImagePreferences


class ImagePreferenceService:
    def __init__(self, repo) -> None:
        self.repo = repo
        self._locks: dict[int, asyncio.Lock] = {}

    async def get(self, user_id: int) -> ImagePreferences:
        stored = await self.repo.get_image_preferences(user_id)
        return ImagePreferences.model_validate(stored or {})

    async def update(
        self, user_id: int, mutate: Callable[[ImagePreferences], ImagePreferences]
    ) -> ImagePreferences:
        # Single-process Telethon architecture: serialize simultaneous callbacks per operator.
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            result = mutate(await self.get(user_id))
            result = ImagePreferences.model_validate(result.model_dump())
            await self.repo.save_image_preferences(
                user_id, result.model_dump(mode="json")
            )
            return result
