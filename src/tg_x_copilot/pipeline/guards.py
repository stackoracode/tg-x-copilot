"""Deterministic checks on model output. Models self-report too, but these run regardless.

They catch the most common failure modes: over-length, clickbait phrases, hashtag/emoji spam,
shouting, numbers that do not appear in the source (fabrication signal), and drafts that are
just the source restated.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from ..models import RewriteResult

_URL = re.compile(r"https?://\S+")
_HASHTAG = re.compile(r"(?<!\w)#\w+")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CAPS_WORD = re.compile(r"\b[A-Z]{4,}\b")
_ALLOWED_CAPS = {"NASA", "NATO", "FIFA", "NCAA", "IRS", "HTTP", "HTML", "COVID", "USDA", "OPEC",
                 "AI", "CEO", "GDP", "NYSE", "SEC", "FBI", "CDC", "EU", "UK", "USA", "NVDA",
                 "AAPL", "TSLA", "MSFT", "GOOG", "AMZN", "META", "API", "GPU", "CPU", "LLM"}


@dataclass(frozen=True)
class XRules:
    max_chars: int = 280
    max_hashtags: int = 1
    max_emojis: int = 2
    max_images: int = 4
    similarity_threshold: float = 0.72
    banned_phrases: tuple[str, ...] = ()
    style: str = "Plain, conversational, concrete."

    @classmethod
    def from_db(cls, rows: dict[str, Any]) -> "XRules":
        known = {k: v for k, v in rows.items() if k in cls.__dataclass_fields__}
        if "banned_phrases" in known:
            known["banned_phrases"] = tuple(str(p) for p in known["banned_phrases"])
        return cls(**known)


@dataclass
class GuardReport:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _is_wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def x_length(text: str) -> int:
    """Approximation of X's weighted length: URLs count 23, CJK/wide chars count 2."""
    text = _URL.sub("x" * 23, text)
    return sum(2 if _is_wide(ch) else 1 for ch in text)


def _is_emoji(ch: str) -> bool:
    return unicodedata.category(ch) == "So" or 0x1F300 <= ord(ch) <= 0x1FAFF


def _norm_number(s: str) -> str:
    return s.replace(",", "").rstrip(".")


def numbers_in(text: str) -> set[str]:
    return {_norm_number(n) for n in _NUMBER.findall(text)}


def similarity(a: str, b: str) -> float:
    a, b = " ".join(a.lower().split()), " ".join(b.lower().split())
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def check_rewrite(result: RewriteResult, *, source_text: str, rules: XRules,
                  allowed_facts: list[str] | None = None) -> GuardReport:
    report = GuardReport()
    post = result.post.strip()
    lower = post.lower()

    if not post:
        report.problems.append("Post is empty.")
        return report
    if not result.hook.strip():
        report.problems.append("Missing hook.")

    length = x_length(post)
    if length > rules.max_chars:
        report.problems.append(f"Post is {length} characters; limit is {rules.max_chars}.")

    for phrase in rules.banned_phrases:
        if phrase.lower() in lower:
            report.problems.append(f"Uses banned clickbait phrase: {phrase!r}.")

    hashtags = _HASHTAG.findall(post)
    if len(hashtags) > rules.max_hashtags:
        report.problems.append(f"{len(hashtags)} hashtags; max is {rules.max_hashtags}.")

    emojis = sum(1 for ch in post if _is_emoji(ch))
    if emojis > rules.max_emojis:
        report.problems.append(f"{emojis} emojis; max is {rules.max_emojis}.")

    shouting = [w for w in _CAPS_WORD.findall(post) if w not in _ALLOWED_CAPS]
    if len(shouting) >= 2:
        report.problems.append(f"ALL-CAPS shouting: {', '.join(shouting[:5])}.")

    if result.is_mere_translation:
        report.problems.append("Model reports the post merely restates/translates the source.")
    if not result.added_value.strip():
        report.problems.append("No added value described beyond the source.")

    sim = similarity(source_text, post)
    if sim >= rules.similarity_threshold:
        report.problems.append(f"Too close to the source text (similarity {sim:.2f}).")

    # Fabrication signal: every number in the post must appear in the source or allowed facts.
    known = numbers_in(source_text)
    for fact in allowed_facts or []:
        known |= numbers_in(fact)
    unknown = sorted(n for n in numbers_in(post) if n not in known)
    if unknown:
        report.problems.append(
            "Numbers not found in source or approved facts (possible fabrication): "
            + ", ".join(unknown)
        )

    background = [c.text for c in result.claims if c.basis == "background"]
    if background:
        report.warnings.append(f"{len(background)} background claim(s) need a quick fact-check.")
    return report
