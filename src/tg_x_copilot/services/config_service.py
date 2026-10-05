"""Runtime configuration = env bootstrap + whitelisted DB overrides."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from ..config import EDITABLE_KEYS, AppSettings, apply_overrides, get_path, secret_values
from ..db import Repository
from ..logging_setup import ctx, register_secrets

log = logging.getLogger(__name__)


class ConfigService:
    def __init__(self, base: AppSettings, repo: Repository) -> None:
        self.base = base
        self.repo = repo
        self.current = base
        self.overrides: dict[str, Any] = {}
        self._listeners: list[Callable[[AppSettings], None]] = []
        register_secrets(secret_values(base))

    def on_change(self, fn: Callable[[AppSettings], None]) -> None:
        self._listeners.append(fn)

    def _apply(self, overrides: dict[str, Any]) -> None:
        self.current = apply_overrides(self.base, overrides)
        self.overrides = overrides
        register_secrets(secret_values(self.current))
        for fn in self._listeners:
            fn(self.current)

    async def load(self) -> None:
        stored = await self.repo.get_settings()
        valid = {k: v for k, v in stored.items() if k in EDITABLE_KEYS}
        try:
            self._apply(valid)
        except ValidationError:
            log.exception("stored settings invalid; falling back to env-only settings")
            self._apply({})
        log.info("settings loaded", extra=ctx(overrides=sorted(valid)))

    @staticmethod
    def parse_value(key: str, raw: str, current: Any) -> Any:
        """Parse a form string into the type of the current value."""
        raw = raw.strip()
        if isinstance(current, bool):
            return raw.lower() in ("1", "true", "yes", "on")
        if isinstance(current, list):
            if raw.startswith("["):
                return json.loads(raw)
            return [int(x) for x in raw.replace(",", " ").split()]
        if current is None and raw == "":
            return None
        return raw

    async def update(self, changes: dict[str, Any]) -> list[str]:
        """Validate and persist changes. Raises ValueError/ValidationError on bad input."""
        unknown = [k for k in changes if k not in EDITABLE_KEYS]
        if unknown:
            raise ValueError(f"not editable: {', '.join(unknown)}")
        merged = {**self.overrides, **changes}
        validated = apply_overrides(self.base, merged)  # validate before persisting
        # store the validated (typed) values
        to_store = {k: (_jsonable(get_path(validated, k)), EDITABLE_KEYS[k]) for k in changes}
        await self.repo.set_settings(to_store)
        self._apply({**self.overrides, **{k: v for k, (v, _) in to_store.items()}})
        log.info("settings updated", extra=ctx(keys=sorted(changes)))
        return sorted(changes)

    async def reset(self, key: str) -> None:
        await self.repo.delete_setting(key)
        self._apply({k: v for k, v in self.overrides.items() if k != key})

    def view(self) -> list[dict[str, Any]]:
        """Editable settings for the admin UI; secrets are masked."""
        rows = []
        for key, secret in EDITABLE_KEYS.items():
            value = get_path(self.current, key)
            if secret:
                shown = "•••••• (set)" if value else ""
            elif isinstance(value, list):
                shown = ", ".join(str(v) for v in value)
            else:
                shown = "" if value is None else str(value)
            rows.append({"key": key, "value": shown, "secret": secret,
                         "overridden": key in self.overrides,
                         "is_bool": isinstance(value, bool)})
        return rows


def _jsonable(v: Any) -> Any:
    return v if isinstance(v, (str, int, float, bool, list, type(None))) else str(v)
