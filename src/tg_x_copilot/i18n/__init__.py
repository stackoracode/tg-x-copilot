"""Locale packs.

A locale pack (`locales/<code>.json`) holds everything language/market specific that is not a
prompt: UI strings for the bot/admin, language name passed to models (so generated images and
text use the configured language), and market defaults. Prompts live in `prompts/<code>/`.

Adding a locale = add `locales/xx-YY.json` + `prompts/xx-YY/` + seed rows in x_rules / hooks.
Missing keys fall back to en-US.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FALLBACK = "en-US"
_DIR = Path(__file__).parent / "locales"


@dataclass(frozen=True)
class Locale:
    code: str
    language_name: str  # passed to models, e.g. "English (US)"
    language_tag: str  # ISO 639-1, e.g. "en"; compared with detected text language in images
    market: str
    timezone: str
    ui: dict[str, str] = field(default_factory=dict)


class I18n:
    def __init__(self, directory: Path = _DIR) -> None:
        self._locales: dict[str, Locale] = {}
        for path in sorted(directory.glob("*.json")):
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            self._locales[raw["code"]] = Locale(
                code=raw["code"],
                language_name=raw["language_name"],
                language_tag=raw["language_tag"],
                market=raw["market"],
                timezone=raw.get("timezone", "UTC"),
                ui=raw.get("ui", {}),
            )
        if FALLBACK not in self._locales:
            raise RuntimeError(f"fallback locale {FALLBACK} missing in {directory}")

    @property
    def codes(self) -> list[str]:
        return sorted(self._locales)

    def get(self, code: str | None) -> Locale:
        return self._locales.get(code or FALLBACK) or self._locales[FALLBACK]

    def t(self, code: str | None, key: str, **kwargs: Any) -> str:
        template = self.get(code).ui.get(key) or self._locales[FALLBACK].ui.get(key) or key
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template


_catalog = I18n()


def t(code: str | None, key: str, **kwargs: Any) -> str:
    """Shared translations for pure policy functions and transport-independent diagnostics."""
    return _catalog.t(code, key, **kwargs)


def label(code: str | None, value: str) -> str:
    key = "label_" + value
    translated = t(code, key)
    return value if translated == key else translated
