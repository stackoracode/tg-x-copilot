"""Local staging for images uploaded through the web compose page.

Telegram keeps forwarded originals in the chat, so the pipeline can always re-download them.
Web uploads have no such home, so the source files are kept here on local disk until the
operator closes the task (publish / not publish / reject) or `web.inbox_ttl_days` passes:

    <inbox_dir>/<task_id>/<idx>   (raw uploaded bytes, already validated as images)

Not R2 (keeps the free-tier budget for final assets only) and not MySQL. All methods are
synchronous and must be called through `asyncio.to_thread`. Single-host by design: every
worker of this deployment runs on the machine that received the upload.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

_TASK_ID = re.compile(r"^[0-9a-f]{32}$")


class WebInbox:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _dir(self, task_id: str) -> Path:
        if not _TASK_ID.fullmatch(task_id):  # never build paths from untrusted input
            raise ValueError("invalid task id")
        return self.root / task_id

    def save(self, task_id: str, idx: int, data: bytes) -> None:
        if idx < 0:
            raise ValueError("invalid media index")
        folder = self._dir(task_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = folder / f".{idx}.part"
        tmp.write_bytes(data)
        os.replace(tmp, folder / str(idx))  # atomic: readers never see a partial file

    def load(self, task_id: str, indices: list[int]) -> dict[int, bytes]:
        folder = self._dir(task_id)
        out: dict[int, bytes] = {}
        for idx in indices:
            path = folder / str(int(idx))
            if path.is_file():
                out[idx] = path.read_bytes()
        return out

    def delete(self, task_id: str) -> bool:
        folder = self._dir(task_id)
        if not folder.exists():
            return False
        shutil.rmtree(folder, ignore_errors=True)
        return True

    def purge(self, max_age_seconds: float, *, now: float | None = None) -> int:
        """Remove task folders older than `max_age_seconds` (by modification time)."""
        if not self.root.is_dir():
            return 0
        cutoff = (now if now is not None else time.time()) - max_age_seconds
        removed = 0
        for folder in self.root.iterdir():
            if folder.is_dir() and _TASK_ID.fullmatch(folder.name) \
                    and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed
