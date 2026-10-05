"""R2 asset store: content-addressed, deduplicated, budget-aware, and reference-counted via MySQL.

Only called for tasks that produced a draft. Nothing from skipped/failed tasks reaches R2, and
objects are deleted again when a task is rejected/regenerated (review copies also on approve),
unless another task still references the same content hash.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..logging_setup import ctx
from ..pipeline.media import ImageBlob

if TYPE_CHECKING:
    from ..app_context import AppContext

log = logging.getLogger(__name__)


class StorageBudgetExceeded(Exception):
    pass


def asset_key(blob: ImageBlob) -> str:
    return f"assets/{blob.sha256[:2]}/{blob.sha256}.{blob.ext}"


class AssetStore:
    def __init__(self, app: "AppContext") -> None:
        self.app = app

    async def persist(self, task_id: str, idx: int, blob: ImageBlob, kind: str) -> str:
        """Upload (or reuse) the blob and attach it to task_media(task_id, idx)."""
        repo, r2 = self.app.repo, self.app.hub.r2
        key = asset_key(blob)
        if not await repo.asset_key_referenced(key):
            budget = self.app.config.current.storage.budget_bytes
            used = await repo.stored_bytes()
            if used + blob.size > budget:
                raise StorageBudgetExceeded(
                    f"R2 budget reached ({used}/{budget} bytes); asset not stored")
            async with self.app.limits.io:
                if not await r2.exists(key):  # object may survive a deleted DB row
                    await r2.put_object(key, blob.data, blob.mime)
            log.info("asset stored", extra=ctx(key=key, bytes=blob.size, kind=kind))
        else:
            log.info("asset deduplicated", extra=ctx(key=key, kind=kind))
        await repo.set_media_asset(task_id, idx, key=key, kind=kind, size=blob.size,
                                   mime=blob.mime)
        return key

    async def release_task(self, task_id: str, kinds: tuple[str, ...] = ("final", "review")
                           ) -> int:
        """Detach this task's assets of the given kinds; delete objects nobody references."""
        repo, r2 = self.app.repo, self.app.hub.r2
        deleted = 0
        for m in await repo.list_media(task_id):
            key = m.get("asset_key")
            if not key or m.get("asset_kind") not in kinds:
                continue
            await repo.clear_media_asset(task_id, m["idx"])
            if await repo.asset_key_referenced(key):
                continue
            try:
                async with self.app.limits.io:
                    await r2.delete_object(key)
                deleted += 1
            except Exception:
                log.exception("R2 delete failed; object orphaned", extra=ctx(key=key))
        if deleted:
            log.info("assets released", extra=ctx(task_id=task_id, deleted=deleted))
        return deleted
