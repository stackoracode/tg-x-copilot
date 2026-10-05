"""asyncio.Queue + background workers with error isolation and graceful shutdown.

- The queue carries task IDs only; state lives in MySQL, so a restart loses nothing:
  tasks still RECEIVED/PROCESSING are re-enqueued on startup.
- One failing task never kills a worker; it is marked FAILED and the worker moves on.
- stop(): idle workers are cancelled immediately, busy ones get `grace` seconds to finish;
  anything cancelled stays PROCESSING and is recovered on next start.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..logging_setup import ctx, task_id_var

log = logging.getLogger(__name__)


class WorkerPool:
    def __init__(
        self,
        handler: Callable[[str], Awaitable[None]],
        on_error: Callable[[str, BaseException], Awaitable[None]],
        *,
        workers: int,
        maxsize: int,
        task_timeout: Callable[[], float],
    ) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        self._handler = handler
        self._on_error = on_error
        self._n = workers
        self._task_timeout = task_timeout
        self._workers: list[asyncio.Task[None]] = []
        self._busy: dict[int, str] = {}
        self._queued: set[str] = set()
        self._stopping = False

    @property
    def stats(self) -> dict[str, object]:
        return {"queued": self.queue.qsize(), "busy": dict(self._busy), "workers": self._n,
                "stopping": self._stopping}

    def start(self) -> None:
        for i in range(self._n):
            self._workers.append(asyncio.create_task(self._run(i), name=f"worker-{i}"))
        log.info("workers started", extra=ctx(workers=self._n))

    async def enqueue(self, task_id: str, *, wait: bool = True) -> bool:
        """Returns False if stopping, already queued/running, or (wait=False) queue full."""
        if self._stopping or task_id in self._queued or task_id in self._busy.values():
            return False
        if wait:
            await self.queue.put(task_id)
        else:
            try:
                self.queue.put_nowait(task_id)
            except asyncio.QueueFull:
                return False
        self._queued.add(task_id)
        return True

    async def _run(self, n: int) -> None:
        while not self._stopping:
            task_id = await self.queue.get()
            self._queued.discard(task_id)
            self._busy[n] = task_id
            token = task_id_var.set(task_id)
            try:
                await asyncio.wait_for(self._handler(task_id), timeout=self._task_timeout())
            except asyncio.CancelledError:
                log.warning("task cancelled (shutdown); will be recovered on restart")
                raise
            except Exception as exc:  # error isolation: never let one task kill the worker
                if isinstance(exc, TimeoutError):
                    exc = TimeoutError(f"task exceeded {self._task_timeout():.0f}s")
                log.exception("task failed", extra=ctx(worker=n))
                await self._on_error(task_id, exc)
            finally:
                task_id_var.reset(token)
                self._busy.pop(n, None)
                self.queue.task_done()

    async def stop(self, grace: float) -> None:
        self._stopping = True
        busy_ids = set(self._busy)
        idle = [t for i, t in enumerate(self._workers) if i not in busy_ids]
        busy = [t for i, t in enumerate(self._workers) if i in busy_ids]
        for t in idle:
            t.cancel()
        if busy:
            log.info("waiting for in-flight tasks", extra=ctx(n=len(busy), grace=grace))
            _, pending = await asyncio.wait(busy, timeout=grace)
            for t in pending:
                t.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        log.info("workers stopped", extra=ctx(left_in_queue=self.queue.qsize()))
