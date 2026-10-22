"""Shared form handling for image intent (compose page and task-page image tools)."""

from __future__ import annotations

from typing import Any

from ..image_settings import (
    DEFAULT_OPTIONS, PRESETS, ImageAction, ImageOption, ImageOptions, ImagePreferences,
    InformationDensity, WorkflowMode,
)

# Actions offered on the web. "" = automatic (rights-aware policy decides per image).
WEB_ACTIONS = [ImageAction.KEEP, ImageAction.ENHANCE, ImageAction.LOCALIZE, ImageAction.RECREATE,
               ImageAction.CLEAN_RECREATE, ImageAction.INFO_CARD, ImageAction.GENERATE,
               ImageAction.TEXT_ONLY]


def preferences_from_form(form: Any, *, authorized_media: bool) -> ImagePreferences:
    """Image intent from the compose / image-tools form. Raises ValueError on bad input
    (unknown action/option, conflicting options)."""
    raw_action = str(form.get("image_action") or "").strip()
    action = ImageAction(raw_action) if raw_action else None
    if form.get("custom_options"):
        flags = frozenset(ImageOption(str(v)) for v in form.getlist("flag"))
    else:
        flags = PRESETS.get(action, DEFAULT_OPTIONS) if action else DEFAULT_OPTIONS
    target = str(form.get("target_locale") or "").strip() or None
    options = ImageOptions(
        flags=flags,
        information_density=InformationDensity(str(form.get("density") or "medium")),
        target_locale=target,
    )
    mode = WorkflowMode.QUICK if form.get("mode") == "quick" else WorkflowMode.MANUAL
    return ImagePreferences(workflow_mode=mode, authorized_media=authorized_media,
                            image_action=action, image_options=options)


def form_context(ctx: Any, prefs: ImagePreferences) -> dict[str, Any]:
    return {
        "actions": [a.value for a in WEB_ACTIONS],
        "options": [o.value for o in ImageOption],
        "densities": [d.value for d in InformationDensity],
        "locales": ctx.i18n.codes,
        "prefs": prefs,
        "max_files": ctx.config.current.web.max_files,
        "max_mb": ctx.config.current.pipeline.max_media_bytes // (1024 * 1024),
        "fetch_enabled": ctx.config.current.web.fetch_urls,
        "fetch_ai_available": bool(ctx.config.current.web.fetch_llm_model)
                              and "llm" in ctx.config.current.web.fetch_strategies,
        "quick_mode": prefs.workflow_mode == WorkflowMode.QUICK,
    }
