"""Rights-aware policy. Rebuilding information never means erasing a source watermark."""
from __future__ import annotations

from dataclasses import dataclass

from ..image_settings import ACTIONS, ImageAction, ImageOption, ImageOptions, MediaCategory
from ..i18n import t
from ..models import ImageAnalysis, ImageDecision, InputEnvelope, SourceMedia
from .overlays import approved_regions, wants_region_edit

_REDRAWABLE = {'chart', 'infographic', 'illustration', 'screenshot', 'photo_generic', 'meme',
               'ui_screenshot', 'mixed_layout', 'generic_visual', 'brand_asset'}


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
    overlay = analysis.has_channel_overlay or (analysis.has_third_party_watermark and
        analysis.has_source_copyright_mark is None and not analysis.mark_regions)
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
        if wrong_language and analysis.image_type in {'chart', 'infographic', 'screenshot', 'ui_screenshot', 'mixed_layout'}:
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


# Primary actions resolve into a handful of reusable execution strategies.


@dataclass(frozen=True)
class ImagePlan:
    action: ImageAction | None
    execution: str
    qc_mode: str | None
    reason: str


def plan(analysis: ImageAnalysis, *, requested: ImageAction | None, options: ImageOptions,
         owned: bool, target_language: str, locale: str, idx: int = 0) -> ImagePlan:
    if wants_region_edit(requested, options) and requested not in (ImageAction.OMIT, ImageAction.TEXT_ONLY):
        if not owned:
            return ImagePlan(None, 'review', None, t(locale, 'cleanup_rights_required'))
        if analysis.sensitive or analysis.relevance < .3:
            return ImagePlan(None, 'review', None, t(locale, 'policy_sensitive'))
        if not analysis.has_channel_overlay and not analysis.mark_regions and not analysis.has_third_party_watermark:
            return ImagePlan(ImageAction.KEEP, 'keep', None, t(locale, 'cleanup_no_promotion'))
        try:
            approved_regions(analysis, options, idx)
        except ValueError:
            return ImagePlan(None, 'review', None, t(locale, 'cleanup_scope_required'))
        return ImagePlan(ImageAction.CLEAN_RECREATE, 'edit', 'promotion_cleanup', t(locale, 'cleanup_authorized'))
    if requested is None:
        decision, reason = decide(analysis, owned_source=owned,
                                  target_language=target_language, locale=locale)
        if decision == ImageDecision.REVIEW:
            return ImagePlan(None, 'review', None, reason)
        action = ImageAction(decision.value)
    else:
        action = requested
        reason = t(locale, 'image_action_reason', action=t(locale, 'image_action_' + action.value))
    spec = ACTIONS[action]
    if spec.execution == 'omit':
        return ImagePlan(action, 'omit', None, reason)
    if analysis.sensitive or analysis.relevance < .3:
        return ImagePlan(None, 'review', None, t(locale, 'policy_sensitive' if analysis.sensitive
                                               else 'policy_unrelated'))
    if spec.execution == 'keep' or (spec.execution == 'edit' and spec.qc_mode == 'enhance'):
        if not owned:
            return ImagePlan(None, 'review', None, t(locale, 'image_rights_required'))
        foreign = analysis.contains_text and (not analysis.text_language or
            analysis.text_language.split('-')[0].lower() != target_language.lower() or
            (target_language == 'zh' and analysis.text_script != 'simplified'))
        if foreign:
            return ImagePlan(None, 'review', None, t(locale, 'policy_localize'))
    execution, qc_mode = spec.execution, spec.qc_mode
    flexible_information = category(analysis.image_type) in {
        MediaCategory.UI_SCREENSHOT, MediaCategory.INFOGRAPHIC, MediaCategory.MIXED_LAYOUT}
    redesign_localization = (qc_mode == 'localize' and flexible_information and
                              ImageOption.REDESIGN in options.flags)
    if execution == 'edit' and (not owned or redesign_localization):
        execution, qc_mode = 'create', 'regenerate'
    documentary = analysis.image_type == 'photo_real_event' or analysis.depicts_real_people
    if execution == 'create' and documentary:
        action = ImageAction.INFO_CARD
        reason = t(locale, 'policy_news_card')
    return ImagePlan(action, execution, qc_mode, reason)


def category(image_type: str | None) -> MediaCategory:
    """Normalize legacy analysis types without changing stored source records."""
    aliases = {'photo_real_event': MediaCategory.DOCUMENTARY, 'screenshot': MediaCategory.UI_SCREENSHOT,
               'chart': MediaCategory.INFOGRAPHIC}
    if image_type in aliases:
        return aliases[image_type]
    return MediaCategory._value2member_map_.get(image_type, MediaCategory.GENERIC_VISUAL)
