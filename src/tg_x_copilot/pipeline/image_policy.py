"""Rights-aware policy. Rebuilding information never means erasing a source watermark."""
from __future__ import annotations

from ..i18n import t
from ..models import ImageAnalysis, ImageDecision, InputEnvelope, SourceMedia

_REDRAWABLE = {'chart', 'infographic', 'illustration', 'screenshot', 'photo_generic', 'meme'}


def decide(analysis: ImageAnalysis, *, owned_source: bool, target_language: str,
           locale: str = 'en-US') -> tuple[ImageDecision, str]:
    if analysis.sensitive:
        return ImageDecision.REVIEW, t(locale, 'policy_sensitive')
    if analysis.relevance < 0.3:
        return ImageDecision.REVIEW, t(locale, 'policy_unrelated')
    wrong_language = bool(analysis.contains_text and (
        not analysis.text_language or
        analysis.text_language.lower().split('-')[0] != target_language.lower().split('-')[0] or
        (target_language.lower().split('-')[0] == 'zh' and
         analysis.text_script != 'simplified')))
    overlay = analysis.has_channel_overlay or analysis.has_third_party_watermark
    real_world = analysis.image_type == 'photo_real_event' or analysis.depicts_real_people
    # Unlicensed photos are never edited/regenerated as photographs. Original cards use facts
    # only, without a source image sent to the generator or a real person's likeness.
    if real_world and (not owned_source or overlay):
        return ImageDecision.RECREATE, t(locale, 'policy_news_card')
    if overlay:
        return ImageDecision.RECREATE, t(locale, 'policy_recreate')
    if owned_source:
        if wrong_language:
            return ImageDecision.LOCALIZE, t(locale, 'policy_localize')
        if analysis.quality == 'low':
            if real_world:
                return ImageDecision.ENHANCE, t(locale, 'policy_enhance')
            return ImageDecision.RECREATE, t(locale, 'policy_recreate')
        return ImageDecision.KEEP, t(locale, 'policy_owned')
    if analysis.image_type in _REDRAWABLE:
        # LOCALIZE for third-party information is an original redraw, never an image edit.
        if wrong_language and analysis.image_type in {'chart', 'infographic', 'screenshot'}:
            return ImageDecision.LOCALIZE, t(locale, 'policy_localize')
        return ImageDecision.RECREATE, t(locale, 'policy_recreate')
    return ImageDecision.REVIEW, t(locale, 'policy_unknown')


def media_is_owned(media: SourceMedia, env: InputEnvelope, *, owned_ids: set[int],
                   direct_uploads_owned: bool) -> bool:
    """Confirm rights per image, including mixed-origin albums. Unknown origin fails closed."""
    if media.forwarded is True:
        return media.source_chat_id is not None and media.source_chat_id in owned_ids
    if media.forwarded is False:
        return direct_uploads_owned
    # Old tasks contain only envelope-level attribution: every origin must be confirmed.
    if env.forwards:
        return all(f.chat_id is not None and f.chat_id in owned_ids for f in env.forwards)
    return direct_uploads_owned
