"""Composition root: builds every component once and owns startup/shutdown order.

Start:  DB -> settings overrides -> clients -> Telegram -> workers -> sweeper
Stop:   workers (grace; claims of cancelled tasks are released) -> Telegram -> sweeper -> HTTP -> DB

Channels: Telegram (bot intake) and web (compose page); both end in `submit()`.
Either can run a task as "quick create" (WorkflowMode.QUICK / `/quick`): see pipeline/quick.py.

Grouping: manual mode keeps one task per message or album. Per-user automatic bundle mode
explicitly groups nearby forwards before durable intake; copyright/edit permissions stay separate.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from typing import Any

from .bot import TelegramBot
from .config import AppSettings
from .db import Database, Repository
from .i18n import I18n
from .logging_setup import ctx
from .pipeline.limits import Limits
from .pipeline.normalizer import normalize
from .pipeline.processor import Pipeline
from .pipeline.worker import WorkerPool
from .services.config_service import ConfigService
from .services.hub import ClientHub
from .services.ops import Ops
from .services.image_preferences import ImagePreferenceService
from .services.link_reader import LinkReader
from .services.storage import AssetStore
from .services.web_inbox import WebInbox
from .models import InputEnvelope
from .pipeline.media import ImageBlob
from .image_settings import ImagePreferences, WorkflowMode, resolve_workflow, has_image_source

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 60.0


class AppContext:
    def __init__(self, settings: AppSettings) -> None:
        # Unique per process: used as the owner of atomic task claims in MySQL.
        self.instance_id = f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.shutting_down = False  # cancelled tasks release their claim only during shutdown
        self.limits = Limits.from_settings(settings.concurrency)
        self.db = Database(settings.db, self.limits.db)
        self.repo = Repository(self.db)
        self.config = ConfigService(settings, self.repo)
        self.i18n = I18n()
        self.hub = ClientHub()
        self.hub.configure(settings)
        self.config.on_change(self.hub.configure)
        self.storage = AssetStore(self)
        self.pipeline = Pipeline(self)
        self.ops = Ops(self)
        self.image_preferences = ImagePreferenceService(self.repo)
        self.web_inbox = WebInbox(settings.web.inbox_dir)
        self.links = LinkReader(self)
        self._last_inbox_purge = float("-inf")  # first sweep purges immediately
        self.workers = WorkerPool(
            self.pipeline.process, self.pipeline.mark_failed,
            workers=settings.concurrency.workers, maxsize=settings.concurrency.queue_maxsize,
            task_timeout=lambda: self.config.current.pipeline.task_timeout_seconds,
        )
        self.telegram: TelegramBot | None = None
        self._sweeper: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ intake

    async def intake(self, chat_id: int, user_id: int, messages: list[Any], *,
                     preferences: ImagePreferences | None = None,
                     status_message_id: int | None = None) -> None:
        """Telegram intake: one message or album -> one task."""
        cfg = self.config.current
        env = normalize(messages, chat_id=chat_id, user_id=user_id,
                        locale=cfg.default_locale, market=cfg.market)
        preferences = preferences or await self.image_preferences.get(user_id)
        await submit_envelope(self, env, preferences, status_message_id=status_message_id)

    async def submit(self, env: InputEnvelope, preferences: ImagePreferences, *,
                     task_id: str | None = None,
                     status_message_id: int | None = None) -> str | None:
        return await submit_envelope(self, env, preferences, task_id=task_id,
                                     status_message_id=status_message_id)

    async def submit_web(self, env: InputEnvelope, preferences: ImagePreferences,
                         images: list[ImageBlob]) -> str | None:
        """Web intake: stage uploads on local disk under a fresh task id, then create the task.
        The row only exists once its media is on disk; a failed submission leaves nothing."""
        task_id = uuid.uuid4().hex
        try:
            for idx, blob in enumerate(images):
                await asyncio.to_thread(self.web_inbox.save, task_id, idx, blob.data)
            created = await submit_envelope(self, env, preferences, task_id=task_id)
        except BaseException:
            await asyncio.to_thread(self.web_inbox.delete, task_id)
            raise
        if created is None:
            await asyncio.to_thread(self.web_inbox.delete, task_id)
        return created

    # ------------------------------------------------------------------ recovery

    def _lease_seconds(self) -> int:
        # A claim older than the task timeout (+ margin) belongs to a crashed worker/process.
        return int(self.config.current.pipeline.task_timeout_seconds) + 120

    async def _purge_inbox(self) -> None:
        """Hourly: drop web uploads of tasks nobody closed within web.inbox_ttl_days."""
        now = time.monotonic()
        if now - self._last_inbox_purge < 3600:
            return
        self._last_inbox_purge = now
        ttl = self.config.current.web.inbox_ttl_days * 86400
        removed = await asyncio.to_thread(self.web_inbox.purge, ttl)
        if removed:
            log.info("purged expired web uploads", extra=ctx(folders=removed))

    async def _sweep_once(self) -> int:
        try:
            await self._purge_inbox()
        except Exception:
            log.exception("web inbox purge failed")
        stale = await self.repo.requeue_stale(self._lease_seconds())
        if stale:
            log.warning("requeued tasks with expired claims", extra=ctx(n=stale))
        n = 0
        for task_id in await self.repo.runnable_task_ids():
            if await self.workers.enqueue(task_id, wait=False):
                n += 1
        return n

    async def _sweep_loop(self) -> None:
        while True:
            try:
                n = await self._sweep_once()
                if n:
                    log.info("sweeper enqueued tasks", extra=ctx(n=n))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sweeper failed")
            await asyncio.sleep(SWEEP_INTERVAL_S)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.db.ping()
        log.info("database connected")
        await self.config.load()
        # Telegram before workers: recovered tasks need it to download media.
        if self.config.base.telegram.enabled:
            self.telegram = TelegramBot(self)
            await self.telegram.start()
        else:
            log.warning("telegram disabled (TELEGRAM__ENABLED=false); admin UI only")
        self.workers.start()
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="sweeper")
        log.info("tg-x-copilot started")

    async def stop(self) -> None:
        grace = self.config.base.shutdown_grace_seconds
        self.shutting_down = True
        log.info("shutting down", extra=ctx(grace=grace))
        # Persist buffered automatic bundles before the workers begin draining.
        if self.telegram:
            try:
                await self.telegram.flush_intake()
            except Exception:
                log.exception("flushing automatic intake failed during shutdown")
        # Workers drain while Telegram is still connected so in-flight drafts get delivered.
        steps = [
            ("workers", lambda: self.workers.stop(grace)),
            ("telegram", self.telegram.stop if self.telegram else None),
        ]
        for name, fn in steps:
            if fn is None:
                continue
            try:
                await fn()
            except Exception:
                log.exception("error stopping %s", name)
        if self._sweeper:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
        for name, closer in (("http", self.hub.close), ("db", self.db.close)):
            try:
                await closer()
            except Exception:
                log.exception("error closing %s", name)
        log.info("shutdown complete")


async def submit_envelope(app: Any, env: InputEnvelope, preferences: ImagePreferences, *,
                          task_id: str | None = None,
                          status_message_id: int | None = None) -> str | None:
    """Channel-independent intake: apply image preferences, create and enqueue the task.
    Returns the task id, or None when there is nothing to process."""
    effective = resolve_workflow(preferences, has_images=has_image_source(env.media))
    env.status_message_id = status_message_id
    env.media_edit_rights_confirmed = effective.authorized_media
    env.workflow_mode = effective.workflow_mode
    env.image_action = effective.image_action
    env.image_options = effective.image_options
    env.quick = env.quick or effective.workflow_mode == WorkflowMode.QUICK
    if not env.text and not env.media:
        return None
    task_id = await app.repo.create_task(env, task_id=task_id)
    await app.repo.add_event(task_id, "intake",
                              f"{env.channel}: {len(env.message_ids)} message(s), "
                              f"{len(env.media)} media",
                              data={"message_ids": env.message_ids, "channel": env.channel})
    log.info("task created", extra=ctx(task_id=task_id, channel=env.channel,
                                       messages=len(env.message_ids)))
    telegram = app.telegram if env.channel == "telegram" else None
    if telegram and status_message_id is not None:
        try:
            await telegram.update_task_status(task_id, "queued")
        except Exception:
            log.exception("queue receipt update failed")
    await app.workers.enqueue(task_id, wait=False)  # sweeper catches overflow
    if telegram and status_message_id is None and env.workflow_mode != WorkflowMode.AUTO_BUNDLE:
        await telegram.notify(env.chat_id, app.i18n.t(
            env.locale, "queued", count=len(env.message_ids), task_id=task_id))
    return task_id
