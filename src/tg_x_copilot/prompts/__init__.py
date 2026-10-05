"""Prompt store: prompts are data files, never inline strings in business logic.

Layout: prompts/<locale>/<name>.md, split into sections by lines `<<<SYSTEM>>>` and `<<<USER>>>`.
Placeholders use `$name` (string.Template) so JSON braces in prompts need no escaping.
Falls back to en-US when a locale has no override for a prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

from ..i18n import FALLBACK

_DIR = Path(__file__).parent


@dataclass(frozen=True)
class RenderedPrompt:
    system: str
    user: str

    def messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.system}, {"role": "user", "content": self.user}]


@lru_cache(maxsize=128)
def _load(locale: str, name: str) -> tuple[str, str]:
    for code in (locale, FALLBACK):
        path = _DIR / code / f"{name}.md"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            break
    else:
        raise FileNotFoundError(f"prompt {name!r} not found for {locale} or {FALLBACK}")
    if "<<<USER>>>" in text:
        system, user = text.split("<<<USER>>>", 1)
    else:
        system, user = "", text
    return system.replace("<<<SYSTEM>>>", "").strip(), user.strip()


def render(name: str, locale: str, **values: Any) -> RenderedPrompt:
    system, user = _load(locale, name)
    str_values = {k: ("" if v is None else str(v)) for k, v in values.items()}
    return RenderedPrompt(
        system=Template(system).safe_substitute(str_values),
        user=Template(user).safe_substitute(str_values),
    )


@lru_cache(maxsize=32)
def _load_json(locale: str, name: str) -> str:
    for code in (locale, FALLBACK):
        path = _DIR / code / f"{name}.json"
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"prompt {name!r}.json not found for {locale} or {FALLBACK}")


def render_json(name: str, locale: str, **values: Any) -> dict[str, Any]:
    """Load a JSON prompt (e.g. Jev question sets), substitute `$vars` inside string values,
    and drop keys starting with `_` (comments)."""

    def sub(node: Any) -> Any:
        if isinstance(node, str):
            return Template(node).safe_substitute({k: str(v) for k, v in values.items()})
        if isinstance(node, list):
            return [sub(x) for x in node]
        if isinstance(node, dict):
            return {k: sub(v) for k, v in node.items() if not k.startswith("_")}
        return node

    return sub(json.loads(_load_json(locale, name)))


def clear_cache() -> None:
    _load.cache_clear()
    _load_json.cache_clear()
