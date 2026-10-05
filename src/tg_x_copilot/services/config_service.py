"""Runtime configuration = env bootstrap + whitelisted, non-secret DB overrides.

API keys (CPA, Jev, R2) and other credentials are environment-only. They are never accepted by
`update()` and are ignored (with a warning) if found in the `settings` table.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from ..config import EDITABLE_KEYS, AppSettings, apply_overrides, get_path, is_secret_key, secret_values
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
        for fn in self._listeners:
            fn(self.current)

    async def load(self) -> None:
        stored = await self.repo.get_settings()
        ignored = sorted(k for k in stored if k not in EDITABLE_KEYS)
        if ignored:
            log.warning("ignoring non-editable settings stored in MySQL (API keys are env-only;"
                        " delete these rows)", extra=ctx(keys=ignored))
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
        secret = [k for k in changes if is_secret_key(k)]
        if secret:
            raise ValueError(f"API keys/credentials are env-only: {', '.join(secret)}")
        unknown = [k for k in changes if k not in EDITABLE_KEYS]
        if unknown:
            raise ValueError(f"not editable: {', '.join(unknown)}")
        merged = {**self.overrides, **changes}
        validated = apply_overrides(self.base, merged)  # validate before persisting
        to_store = {k: _jsonable(get_path(validated, k)) for k in changes}
        await self.repo.set_settings(to_store)
        self._apply({**self.overrides, **to_store})
        log.info("settings updated", extra=ctx(keys=sorted(changes)))
        return sorted(changes)

    async def reset(self, key: str) -> None:
        await self.repo.delete_setting(key)
        self._apply({k: v for k, v in self.overrides.items() if k != key})

    def view(self) -> list[dict[str, Any]]:
        """Editable (non-secret) settings for the admin UI."""
        rows = []
        for key in sorted(EDITABLE_KEYS):
            value = get_path(self.current, key)
            if isinstance(value, list):
                shown = ", ".join(str(v) for v in value)
            else:
                shown = "" if value is None else str(value)
            rows.append({"key": key, "value": shown, "overridden": key in self.overrides,
                         "is_bool": isinstance(value, bool)})
        return rows

    def credentials_status(self) -> dict[str, bool]:
        """Which env-only credentials are set (never their values)."""
        s = self.base
        return {
            "CPA__API_KEY": bool(s.cpa.api_key.get_secret_value()),
            "JEV__API_KEY": bool(s.jev.api_key.get_secret_value()),
            "R2__ACCESS_KEY_ID": bool(s.r2.access_key_id.get_secret_value()),
            "R2__SECRET_ACCESS_KEY": bool(s.r2.secret_access_key.get_secret_value()),
        }


def _jsonable(v: Any) -> Any:
    return v if isinstance(v, (str, int, float, bool, list, type(None))) else str(v)
