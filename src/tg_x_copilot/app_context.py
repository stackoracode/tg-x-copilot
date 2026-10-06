"""Composition root: builds every component once and owns startup/shutdown order.

Start:  DB -> settings overrides -> clients -> Telegram -> workers -> sweeper
Stop:   workers (grace; claims of cancelled tasks are released) -> Telegram -> sweeper -> HTTP -> DB

Grouping: a Telegram album (one grouped_id) becomes one task; every other message, forwarded or
not, becomes its own task. Unrelated forwards are never merged by arrival time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
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
from .services.storage import AssetStore

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
        self.workers = WorkerPool(
            self.pipeline.process, self.pipeline.mark_failed,
            workers=settings.concurrency.workers, maxsize=settings.concurrency.queue_maxsize,
            task_timeout=lambda: self.config.current.pipeline.task_timeout_seconds,
        )
        self.telegram: TelegramBot | None = None
        self._sweeper: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ intake

    async def intake(self, chat_id: int, user_id: int, messages: list[Any]) -> None:
        cfg = self.config.current
        env = normalize(messages, chat_id=chat_id, user_id=user_id,
                        locale=cfg.default_locale, market=cfg.market)
        preferences = await self.image_preferences.get(user_id)
        env.image_action = preferences.image_action
        env.image_options = preferences.image_options
        if not env.text and not env.media:
            return
        task_id = await self.repo.create_task(env)
        await self.repo.add_event(task_id, "intake",
                                  f"{len(env.message_ids)} message(s), {len(env.media)} media",
                                  data={"message_ids": env.message_ids})
        log.info("task created", extra=ctx(task_id=task_id, messages=len(env.message_ids)))
        await self.workers.enqueue(task_id, wait=False)  # sweeper catches overflow
        if self.telegram:
            await self.telegram.notify(chat_id, self.i18n.t(
                env.locale, "queued", count=len(env.message_ids), task_id=task_id))

    # ------------------------------------------------------------------ recovery

    def _lease_seconds(self) -> int:
        # A claim older than the task timeout (+ margin) belongs to a crashed worker/process.
        return int(self.config.current.pipeline.task_timeout_seconds) + 120

    async def _sweep_once(self) -> int:
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
