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

from ..i18n import t
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
    # Blocking: trigger a rewrite retry; a draft that still has them cannot be approved.
    problems: list[str] = field(default_factory=list)
    # Non-blocking but mandatory human review before approval (unverified LLM facts).
    review: list[str] = field(default_factory=list)
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
                  verified_facts: list[str] | None = None,
                  unverified_facts: list[str] | None = None,
                  locale: str = "en-US") -> GuardReport:
    """verified_facts: text taken from the source itself (e.g. text read from its images).
    unverified_facts: LLM-produced material (editor key facts, background points). Numbers
    backed only by these, and any `background` claim, require human review."""
    def tr(key: str, **kw: Any) -> str:
        return t(locale, key, **kw)
    report = GuardReport()
    post = result.post.strip()
    lower = post.lower()
    narrative = [post, result.hook, result.added_value, result.image_brief]
    narrative.extend(c.text for c in result.claims)
    if any(wrong_language(text, locale) for text in narrative if text):
        report.problems.append(tr("guard_language"))

    if not post:
        report.problems.append(tr("guard_empty"))
        return report
    if not result.hook.strip():
        report.problems.append(tr("guard_hook"))

    length = x_length(post)
    if length > rules.max_chars:
        report.problems.append(tr("guard_length", length=length, limit=rules.max_chars))

    for phrase in rules.banned_phrases:
        if phrase.lower() in lower:
            report.problems.append(tr("guard_banned", phrase=phrase))

    hashtags = _HASHTAG.findall(post)
    if len(hashtags) > rules.max_hashtags:
        report.problems.append(tr("guard_hashtags", count=len(hashtags), limit=rules.max_hashtags))

    emojis = sum(1 for ch in post if _is_emoji(ch))
    if emojis > rules.max_emojis:
        report.problems.append(tr("guard_emojis", count=emojis, limit=rules.max_emojis))

    shouting = [w for w in _CAPS_WORD.findall(post) if w not in _ALLOWED_CAPS]
    if len(shouting) >= 2:
        report.problems.append(tr("guard_caps", items=", ".join(shouting[:5])))

    if result.is_mere_translation:
        report.problems.append(tr("guard_translation"))
    if not result.added_value.strip():
        report.problems.append(tr("guard_value"))

    sim = similarity(source_text, post)
    if sim >= rules.similarity_threshold:
        report.problems.append(tr("guard_similarity", value=f"{sim:.2f}"))

    # Fabrication signal: every number must be traceable. Numbers absent everywhere block the
    # draft; numbers backed only by LLM-produced facts need a human check.
    verified = numbers_in(source_text)
    for fact in verified_facts or []:
        verified |= numbers_in(fact)
    unverified: set[str] = set()
    for fact in unverified_facts or []:
        unverified |= numbers_in(fact)
    post_numbers = numbers_in(post)
    unknown = sorted(n for n in post_numbers if n not in verified and n not in unverified)
    if unknown:
        report.problems.append(
            tr("guard_numbers", items=", ".join(unknown))
        )
    llm_only = sorted(n for n in post_numbers if n not in verified and n in unverified)
    if llm_only:
        report.review.append(
            tr("guard_llm_numbers", items=", ".join(llm_only))
        )

    # Background facts added by the LLM are unverified: they require review, not just a warning.
    # Opinions/explanations (basis "opinion") assert no new fact and are allowed as-is.
    for claim in result.claims:
        if claim.basis == "background":
            report.review.append(tr("guard_background", claim=claim.text))
    return report


def wrong_language(text: str, locale: str) -> bool:
    """Catch obvious wrong-language prose. Names and technical tokens remain valid.

    Semantic language checking is also required by editorial and visual QC prompts.
    """
    # Strip URLs/code; Chinese proper names may appear in English posts, but full Chinese
    # sentences must not. Latin names/identifiers are allowed in Chinese prose.
    prose = _URL.sub('', text)
    prose = re.sub(r'`[^`]*`', '', prose)
    cjk = re.findall(r'[\u3400-\u9fff]', prose)
    if locale == 'en-US':
        # Long Chinese company/person names remain valid in English prose.
        # Semantic verification catches untranslated sentences without punctuation.
        return bool(re.search(r'[\u3400-\u9fff]{5,}[，。！？]', prose))
    if locale == 'zh-CN':
        return (not cjk and len(re.findall(r'\b[A-Za-z]+\b', prose)) >= 4) or bool(
            re.search(r'\b(?:the|this|these|there|we|you|it)\s+(?:is|are|was|were|will|should|can)\b', prose, re.I))
    return False


def has_unwanted_publishing_frame(post: str, locale: str) -> bool:
    """Reject boilerplate at paragraph boundaries; never strip legitimate source facts."""
    from .. import prompts
    rules = prompts.render_json("publishing_style", locale)
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", post) if part.strip()]
    sentences = [part.strip() for part in re.split(r"[。.!?！？]", post) if part.strip()]
    tail = paragraphs[-1] if len(paragraphs) > 1 else (sentences[-1] if sentences else "")
    return bool(re.search(rules["opening"], post, re.I) or
                re.search(rules["closing"], tail, re.I))
