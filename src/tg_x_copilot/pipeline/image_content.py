"""Publishing evidence excludes promotion; untouched OCR/mark geometry remains QC evidence."""

from __future__ import annotations

import re

from ..models import ImageAnalysis
from .cleaning import preclean


def promotion_strings(analysis: ImageAnalysis) -> list[str]:
    return sorted(
        {
            mark.text.strip()
            for mark in analysis.mark_regions
            if mark.kind == "promotion" and mark.confidence >= 0.9 and len(mark.text.strip()) >= 4
        },
        key=len,
        reverse=True,
    )


def promotion_signals(analysis: ImageAnalysis) -> list[str]:
    strings = promotion_strings(analysis)
    identifiers = {
        token
        for value in strings
        for token in re.findall(r"@[A-Za-z0-9_]{3,}|https?://[^\s]+", value)
    }
    return sorted(set(strings) | identifiers, key=len, reverse=True)


def contains_promotion(text: str, analysis: ImageAnalysis) -> bool:
    return any(p.casefold() in text.casefold() for p in promotion_signals(analysis))


def without_promotion(text: str, analysis: ImageAnalysis) -> str:
    for promotion in promotion_signals(analysis):
        text = re.sub(re.escape(promotion), "", text, flags=re.I)
    return preclean(text).strip()


def editorial_text(analysis: ImageAnalysis) -> str:
    # Recompute even for old cached analyses; never trust a model-supplied cleaned text as
    # independent evidence. Every remaining character comes from source OCR.
    text = without_promotion(analysis.extracted_text, analysis)
    attribution = {
        mark.text.strip().casefold()
        for mark in analysis.mark_regions
        if mark.kind in ("author", "photographer", "copyright", "media_rights")
        and mark.text.strip()
    }
    return "\n".join(
        line for line in text.splitlines() if line.strip().casefold() not in attribution
    )


def publishing_facts(analysis: ImageAnalysis) -> list[str]:
    promotions = promotion_signals(analysis)
    attribution = {
        mark.text.strip().casefold()
        for mark in analysis.mark_regions
        if mark.kind in ("author", "photographer", "copyright", "media_rights")
    }
    return [
        fact
        for fact in analysis.source_facts
        if fact.strip().casefold() not in attribution
        and not any(p.casefold() in fact.casefold() for p in promotions)
        and preclean(fact).strip() == fact.strip()
    ]


def sanitize_analysis(analysis: ImageAnalysis) -> ImageAnalysis:
    return analysis.model_copy(
        update={
            "content_text": editorial_text(analysis),
            "source_facts": publishing_facts(analysis),
        }
    )
