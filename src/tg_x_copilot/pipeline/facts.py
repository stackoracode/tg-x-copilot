"""Source-confirmed fact packets. Editorial additions are never generation evidence."""

from __future__ import annotations

import json

from .. import prompts
from ..models import FactVerification, ImageAnalysis, InputEnvelope, VerifiedFacts
from .guards import numbers_in
from .image_content import editorial_text
from .language import is_localized


async def build_verified_facts(
    env: InputEnvelope, analyses: dict[int, ImageAnalysis], app
) -> VerifiedFacts:
    locale = app.i18n.get(env.image_options.target_locale or env.locale)
    sources = {
        "core": env.text,
        "images": {
            str(idx): editorial_text(a)
            for idx, a in analyses.items()
            if not a.sensitive and editorial_text(a)
        },
    }
    if not env.text and not sources["images"]:
        return VerifiedFacts()
    cfg = app.config.current
    p = prompts.render(
        "verified_facts",
        locale.code,
        language_name=locale.language_name,
        sources=json.dumps(sources, ensure_ascii=False),
    )
    async with app.limits.text:
        packet = await app.hub.cpa.chat_json(
            cfg.models.text_model,
            p.messages(),
            VerifiedFacts,
            json_mode=cfg.models.json_mode,
        )
    for fact in packet.facts:
        source = (
            env.text
            if fact.source_idx is None
            else sources["images"].get(str(fact.source_idx), "")
        )
        if not fact.evidence.strip() or fact.evidence not in source:
            raise ValueError("fact evidence not found in source")
        if numbers_in(fact.text) - numbers_in(fact.evidence):
            raise ValueError("fact packet invented numbers")
    if not await is_localized([f.text for f in packet.facts], locale, app):
        raise ValueError("fact packet has wrong language")
    if packet.facts:
        p = prompts.render(
            "verify_facts",
            locale.code,
            sources=json.dumps(sources, ensure_ascii=False),
            packet=packet.model_dump_json(),
        )
        async with app.limits.text:
            verdict = await app.hub.cpa.chat_json(
                cfg.models.text_model,
                p.messages(),
                FactVerification,
                json_mode=cfg.models.json_mode,
            )
        if not verdict.passed:
            raise ValueError("fact packet not entailed by evidence")
    return packet


def packet_is_traceable(
    packet: VerifiedFacts, canonical_text: str, analyses: dict[int, ImageAnalysis]
) -> bool:
    """Validate persisted evidence before independent image runs; no new source claims."""
    for fact in packet.facts:
        source = (
            canonical_text
            if fact.source_idx is None
            else (
                editorial_text(analyses[fact.source_idx])
                if fact.source_idx in analyses
                else ""
            )
        )
        if not fact.evidence.strip() or fact.evidence not in source:
            return False
        if numbers_in(fact.text) - numbers_in(fact.evidence):
            return False
    return bool(packet.facts)
