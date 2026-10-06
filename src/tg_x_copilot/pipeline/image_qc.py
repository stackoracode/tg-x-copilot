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
from ..i18n import t
from ..models import ImageQC
from .guards import numbers_in, wrong_language

QCMode = Literal["enhance", "localize", "regenerate", "promotion_cleanup"]

_CHECKS = (
    ("text", "text_consistent"),
    ("language", "language_consistent"),
    ("numbers", "numbers_consistent"),
    ("dates", "dates_consistent"),
    ("names", "names_consistent"),
    ("brands", "brands_consistent"),
    ("identifiers", "identifiers_consistent"),
    ("readability", "readability_ok"),
    ("density", "density_consistent"),
    ("people", "people_consistent"),
    ("watermarks", "watermarks_ok"),
    ("facts", "facts_consistent"),
)


def qc_verdict(qc: ImageQC, *, allowed_texts: list[str], locale: str = "en-US",
               target_locale: str | None = None, mode: QCMode = "regenerate") -> tuple[bool, str]:
    """Pure decision: (passed, human-readable reason)."""
    failed = [label for label, attr in _CHECKS if not getattr(qc, attr)]
    if mode == "promotion_cleanup":
        failed.extend(label for label, attr in (("protected_marks", "protected_marks_preserved"),
            ("promotion_removal", "promotion_removal_valid"), ("outside_regions", "outside_regions_unchanged"))
            if not getattr(qc, attr))
    if mode != "promotion_cleanup" and wrong_language(qc.rendered_text, target_locale or locale) and "language" not in failed:
        failed.append("language")
    allowed: set[str] = set()
    for text in allowed_texts:
        allowed |= numbers_in(text)
    unexpected = sorted(numbers_in(qc.rendered_text) - allowed)

    reasons: list[str] = []
    if failed:
        reasons.append(t(locale, "qc_checks", items=", ".join(t(locale, "check_" + f) for f in failed)))
    if unexpected:
        reasons.append(t(locale, "qc_numbers", items=", ".join(unexpected)))
    if qc.issues:
        reasons.append(t(locale, "qc_issues", items="; ".join(qc.issues[:5])))
    if qc.passed and not reasons:
        return True, t(locale, "qc_pass")
    return False, " | ".join(reasons) or t(locale, "qc_rejected")


def build_messages(mode: QCMode, *, locale: str, market: str, language_name: str, facts: str,
                   reference_text: str, candidate_url: str, reference_url: str | None, density: str = "medium", cleanup_contract: str = ""
                   ) -> list[dict[str, Any]]:
    rules = prompts.render_json("image_qc_modes", locale, language_name=language_name)[mode]
    note = t(locale, "qc_images_pair" if reference_url else "qc_candidate")
    density_rule = prompts.render_json("image_actions", locale)["densities"][density]
    p = prompts.render("image_qc", locale, market=market, language_name=language_name,
                       mode=mode, mode_rules=rules, density=density, density_rules=density_rule, facts=facts or t(locale, "none"),
                       reference_text=reference_text or t(locale, "none"), images_note=note, cleanup_contract=cleanup_contract)
    content: list[dict[str, Any]] = [{"type": "text", "text": p.user}]
    if reference_url:
        content.append({"type": "image_url", "image_url": {"url": reference_url}})
    content.append({"type": "image_url", "image_url": {"url": candidate_url}})
    return [{"role": "system", "content": p.system}, {"role": "user", "content": content}]
