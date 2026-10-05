"""Visual verification of Image2 outputs (enhance / localize / regenerate).

Every image-model output must pass QC before it can become a final asset. QC combines:
1. a vision-model check (prompts/<locale>/image_qc.md) comparing the candidate with the
   reference image (enhance/localize) or with the allowed facts (regenerate), and
2. a deterministic check that every number rendered in the candidate exists in the reference
   text/facts.
Any failed dimension, reported issue, unexpected number, or QC error => the image goes to REVIEW.
"""

from __future__ import annotations

from typing import Any, Literal

from .. import prompts
from ..models import ImageQC
from .guards import numbers_in

QCMode = Literal["enhance", "localize", "regenerate"]

_CHECKS = (
    ("text", "text_consistent"),
    ("numbers", "numbers_consistent"),
    ("dates", "dates_consistent"),
    ("names", "names_consistent"),
    ("people", "people_consistent"),
    ("watermarks", "watermarks_ok"),
    ("facts", "facts_consistent"),
)


def qc_verdict(qc: ImageQC, *, allowed_texts: list[str]) -> tuple[bool, str]:
    """Pure decision: (passed, human-readable reason)."""
    failed = [label for label, attr in _CHECKS if not getattr(qc, attr)]
    allowed: set[str] = set()
    for text in allowed_texts:
        allowed |= numbers_in(text)
    unexpected = sorted(numbers_in(qc.rendered_text) - allowed)

    reasons: list[str] = []
    if failed:
        reasons.append("failed checks: " + ", ".join(failed))
    if unexpected:
        reasons.append("numbers not in reference: " + ", ".join(unexpected))
    if qc.issues:
        reasons.append("issues: " + "; ".join(qc.issues[:5]))
    if qc.passed and not reasons:
        return True, "QC passed"
    return False, " | ".join(reasons) or "QC model did not pass the image"


def build_messages(mode: QCMode, *, locale: str, market: str, language_name: str, facts: str,
                   reference_text: str, candidate_url: str, reference_url: str | None
                   ) -> list[dict[str, Any]]:
    rules = prompts.render_json("image_qc_modes", locale, language_name=language_name)[mode]
    note = ("The first image is the REFERENCE (original), the second is the CANDIDATE."
            if reference_url else "The attached image is the CANDIDATE.")
    p = prompts.render("image_qc", locale, market=market, language_name=language_name,
                       mode=mode, mode_rules=rules, facts=facts or "(none)",
                       reference_text=reference_text or "(none)", images_note=note)
    content: list[dict[str, Any]] = [{"type": "text", "text": p.user}]
    if reference_url:
        content.append({"type": "image_url", "image_url": {"url": reference_url}})
    content.append({"type": "image_url", "image_url": {"url": candidate_url}})
    return [{"role": "system", "content": p.system}, {"role": "user", "content": content}]
