"""Jev (TypeSafe System One) triage: build the typed query and turn typed answers into a route.

Confidence-gated routing (docs.typesafe.ai/patterns/confidence-routing):
- Jev acts alone only when it is confident: a confident low value score or a high promo
  probability skips the task before any media is downloaded or LLM tokens are spent.
- Risk/unverified nouls above threshold force human review (the draft is still produced).
- When Jev is not confident, the task proceeds and is marked `escalated`; the main LLM's
  evaluation then decides suitability. Jev never writes text.
- Jev is text-only and English-first: with no text there is nothing to judge, so the task
  proceeds and the vision/LLM steps decide.
"""

from __future__ import annotations

from typing import Any

from .. import prompts
from ..clients.jev import JevResponse
from ..config import JevSettings
from ..i18n import label, t
from ..models import InputEnvelope, TriageResult

MIN_TEXT_CHARS = 20
_SKIP_TYPES = {"promo", "chat"}


def build_state(env: InputEnvelope, source_info: str) -> dict[str, Any]:
    """Text-only state object for Jev. Media is described, never sent."""
    kinds: dict[str, int] = {}
    for m in env.media:
        kinds[m.kind.value] = kinds.get(m.kind.value, 0) + 1
    return {
        "post_text": env.text[:20000],
        "links": env.urls[:10],
        "attachments": ", ".join(f"{n} {k}" for k, n in kinds.items()) or "none",
        "target_audience": f"X (Twitter) readers in the {env.market} market",
    }


def build_questions(env: InputEnvelope, locale: str) -> dict[str, dict[str, Any]]:
    questions = prompts.render_json("jev_triage", locale, market=env.market)
    if not env.media:
        questions.pop("media_useful", None)
    return questions


def decide_route(resp: JevResponse, cfg: JevSettings, *, has_text: bool, has_media: bool, locale: str = "en-US"
                 ) -> TriageResult:
    a = resp.answers
    value_ans = a["value"]
    value = value_ans.normalized_score()
    conf = value_ans.confidence
    ctype = a.get("content_type")
    flags = {k: float(v.noul) for k, v in a.items() if v.type == "noul" and v.noul is not None}
    media_p = flags.get("media_useful")

    result = TriageResult(
        route="proceed", value=value, confidence=conf,
        content_type=ctype.choice if ctype else None,
        content_type_confidence=ctype.confidence if ctype else None,
        flags=flags, model=resp.model, usage=resp.usage,
        use_media=has_media and (not has_text or media_p is None
                                 or media_p >= cfg.media_useful_min),
    )
    confident = conf is not None and conf >= cfg.confidence_floor

    if not has_text:
        result.escalated = True
        result.reasons.append(t(locale, "jev_no_text"))
        return result

    if flags.get("promotional", 0.0) >= cfg.promo_skip:
        result.route = "skip"
        result.reasons.append(t(locale, "jev_promo", value=f"{flags['promotional']:.2f}"))
        return result
    if confident and value < cfg.min_value:
        result.route = "skip"
        result.reasons.append(t(locale, "jev_low", value=f"{value:.2f}", confidence=f"{conf:.2f}"))
        return result
    if (ctype and ctype.choice in _SKIP_TYPES and ctype.confidence is not None
            and ctype.confidence >= cfg.confidence_floor):
        result.route = "skip"
        result.reasons.append(t(locale, "jev_type", kind=label(locale, ctype.choice), confidence=f"{ctype.confidence:.2f}"))
        return result

    for flag in ("risky", "unverified"):
        if flags.get(flag, 0.0) >= cfg.risk_review:
            result.route = "review"
            result.reasons.append(t(locale, "jev_flag", flag=label(locale, flag), value=f"{flags[flag]:.2f}"))
    if not confident:
        result.escalated = True
        result.reasons.append(t(locale, "jev_uncertain"))
    if has_media and not result.use_media:
        result.reasons.append(t(locale, "jev_media", value=f"{media_p:.2f}"))
    return result


def has_usable_text(env: InputEnvelope) -> bool:
    return len(env.text.strip()) >= MIN_TEXT_CHARS


def without_jev(reason: str, *, has_media: bool) -> TriageResult:
    """Used when Jev is not called (no text): proceed and let vision + LLM decide."""
    return TriageResult(route="proceed", value=0.0, use_media=has_media, escalated=True,
                        reasons=[reason])
