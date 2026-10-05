"""Pure decision function: what to do with each source image.

Principles (enforced in code, not just prompts):
- Never auto-remove third-party watermarks/logos -> REVIEW.
- Never repost a third-party media file as-is -> media we don't own is never KEEP/ENHANCE.
- Never "regenerate" a real news photo or real people: that would fabricate evidence -> REVIEW.
- Text in images must follow the configured locale language -> REGENERATE/localize when possible.
"""

from __future__ import annotations

from ..models import ImageAnalysis, ImageDecision

_REDRAWABLE = {"chart", "infographic", "illustration", "screenshot", "photo_generic"}


def decide(analysis: ImageAnalysis, *, owned_source: bool, target_language: str
           ) -> tuple[ImageDecision, str]:
    if analysis.sensitive:
        return ImageDecision.REVIEW, "Sensitive content; needs a human decision."
    if analysis.has_third_party_watermark:
        mark = f" ({analysis.watermark_text})" if analysis.watermark_text else ""
        return ImageDecision.REVIEW, (
            f"Third-party watermark/logo detected{mark}. Automatic removal is not allowed; "
            "get permission or choose another image."
        )
    if analysis.relevance < 0.3:
        return ImageDecision.REVIEW, "Image looks unrelated to the post."

    wrong_language = bool(
        analysis.contains_text
        and analysis.text_language
        and analysis.text_language.lower().split("-")[0] != target_language.lower()
    )
    real_world = analysis.image_type == "photo_real_event" or analysis.depicts_real_people

    if owned_source:
        if wrong_language:
            if real_world:
                return ImageDecision.REVIEW, (
                    "Own photo of real people/events contains foreign-language text; "
                    "edit the caption overlay manually."
                )
            return ImageDecision.REGENERATE, (
                f"Own visual; re-create with text in {target_language}."
            )
        if analysis.quality == "low" and not real_world:
            return ImageDecision.ENHANCE, "Own visual with low quality; quality pass only."
        return ImageDecision.KEEP, "Own media, good quality, language OK."

    # Third-party media: never reposted as the same file.
    if real_world:
        return ImageDecision.REVIEW, (
            "Third-party photo of real people/events. It cannot be reposted or re-created "
            "(that would fabricate a news image); license it or post without it."
        )
    if analysis.image_type in _REDRAWABLE:
        return ImageDecision.REGENERATE, (
            "Third-party visual; create an original illustration"
            + (f" with text in {target_language}." if analysis.contains_text else ".")
        )
    return ImageDecision.REVIEW, f"Third-party {analysis.image_type}; needs a human decision."
