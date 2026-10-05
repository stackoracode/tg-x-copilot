"""Structured logging with sensitive-data masking.

Usage:
    log = logging.getLogger(__name__)
    log.info("jev scored", extra=ctx(score=0.8, route="text"))

Every record passes through `mask()` after formatting, so secrets never reach stdout/journald,
even when they appear inside exception messages or upstream error bodies.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from typing import Any

task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("task_id", default=None)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=\-]+"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<bot-token>"),
    (
        re.compile(
            r"(?i)((?:password|passwd|secret|api[_-]?key|token|access[_-]?key(?:[_-]?id)?)"
            r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',}&]+"
        ),
        r"\1***",
    ),
    (re.compile(r"(://[^:/\s@]+:)[^@/\s]+@"), r"\1***@"),
    (re.compile(r"(X-Amz-(?:Signature|Credential)=)[^&\s\"]+"), r"\1***"),
    (re.compile(r"(Credential=)[^,\s]+"), r"\1***"),
    (re.compile(r"(Signature=)[0-9a-f]{16,}"), r"\1***"),
]

_secrets: set[str] = set()


def register_secrets(values: list[str]) -> None:
    for v in values:
        if v and len(v) >= 6:
            _secrets.add(v)


def mask(text: str) -> str:
    for s in sorted(_secrets, key=len, reverse=True):
        if s in text:
            text = text.replace(s, "***")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def ctx(**kwargs: Any) -> dict[str, Any]:
    return {"ctx": kwargs}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        task_id = task_id_var.get()
        if task_id:
            payload["task_id"] = task_id
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return mask(json.dumps(payload, ensure_ascii=False, default=str))


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        task_id = task_id_var.get()
        extra = getattr(record, "ctx", None)
        if task_id:
            line += f" task_id={task_id}"
        if isinstance(extra, dict) and extra:
            line += " " + " ".join(f"{k}={v!r}" for k, v in extra.items())
        return mask(line)


def setup_logging(level: str = "INFO", json_mode: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_mode else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Quiet chatty libraries; uvicorn access logs are useful on a VPS so keep them at INFO.
    for name in ("httpx", "httpcore", "telethon"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
