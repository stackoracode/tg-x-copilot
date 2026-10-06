"""Typed, extensible image intent. Registries drive both menus and execution plans."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ImageAction(StrEnum):
    KEEP = "keep"
    ENHANCE = "enhance"
    LOCALIZE = "localize"
    RECREATE = "recreate"
    CLEAN_RECREATE = "clean_recreate"
    INFO_CARD = "info_card"
    GENERATE = "generate"
    OMIT = "omit"
    TEXT_ONLY = "text_only"


class MediaCategory(StrEnum):
    DOCUMENTARY = "documentary"
    UI_SCREENSHOT = "ui_screenshot"
    INFOGRAPHIC = "infographic"
    MIXED_LAYOUT = "mixed_layout"
    GENERIC_VISUAL = "generic_visual"
    BRAND_ASSET = "brand_asset"
    TEXT_INPUT = "text_input"


class ImageOption(StrEnum):
    KEEP_BRANDS = "keep_brands"
    KEEP_IDENTIFIERS = "keep_identifiers"
    TRANSLATE_TEXT = "translate_text"
    SIMILAR_LAYOUT = "similar_layout"
    REDESIGN = "redesign"
    REMOVE_OVERLAYS = "remove_overlays"
    FACTUAL_PRIORITY = "factual_priority"
    VISUAL_PRIORITY = "visual_priority"
    MINIMAL_CHANGES = "minimal_changes"
    INFO_CARD_FALLBACK = "info_card_fallback"


class WorkflowMode(StrEnum):
    MANUAL = "manual"
    AUTO_BUNDLE = "auto_bundle"


class InformationDensity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ActionSpec:
    execution: Literal["keep", "edit", "create", "omit"]
    qc_mode: Literal["enhance", "localize", "regenerate"] | None = None
    text_capable: bool = False


ACTIONS: dict[ImageAction, ActionSpec] = {
    ImageAction.KEEP: ActionSpec("keep"),
    ImageAction.ENHANCE: ActionSpec("edit", "enhance"),
    ImageAction.LOCALIZE: ActionSpec("edit", "localize"),
    ImageAction.RECREATE: ActionSpec("create", "regenerate"),
    ImageAction.CLEAN_RECREATE: ActionSpec("create", "regenerate"),
    ImageAction.INFO_CARD: ActionSpec("create", "regenerate", True),
    ImageAction.GENERATE: ActionSpec("create", "regenerate", True),
    ImageAction.OMIT: ActionSpec("omit"),
    ImageAction.TEXT_ONLY: ActionSpec("omit"),
}

# Options compose. Contradictory layout priorities are resolved in the preference service.
OPTION_CONFLICTS: dict[ImageOption, frozenset[ImageOption]] = {
    ImageOption.SIMILAR_LAYOUT: frozenset({ImageOption.REDESIGN}),
    ImageOption.REDESIGN: frozenset(
        {ImageOption.SIMILAR_LAYOUT, ImageOption.MINIMAL_CHANGES}
    ),
    ImageOption.MINIMAL_CHANGES: frozenset({ImageOption.REDESIGN}),
}
DEFAULT_OPTIONS = frozenset(
    {
        ImageOption.KEEP_BRANDS,
        ImageOption.KEEP_IDENTIFIERS,
        ImageOption.TRANSLATE_TEXT,
        ImageOption.FACTUAL_PRIORITY,
    }
)


class ImageOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flags: frozenset[ImageOption] = DEFAULT_OPTIONS
    promotion_targets: frozenset[str] = frozenset()  # source-index.region-id; empty = all known safe promotion regions
    information_density: InformationDensity = InformationDensity.MEDIUM
    target_locale: Literal["en-US", "zh-CN"] | None = (
        None  # follow task language by default
    )

    @model_validator(mode="after")
    def consistent(self) -> ImageOptions:
        import re
        if any(not re.fullmatch(r"[0-9]{1,3}\.[a-zA-Z0-9_-]{1,16}", key) for key in self.promotion_targets):
            raise ValueError("invalid promotion region selection")
        for flag in self.flags:
            if OPTION_CONFLICTS.get(flag, frozenset()) & self.flags:
                raise ValueError("conflicting image options")
        return self

    def set_flag(self, flag: ImageOption, enabled: bool) -> ImageOptions:
        flags = set(self.flags)
        if enabled:
            flags -= OPTION_CONFLICTS.get(flag, frozenset())
            flags.add(flag)
        else:
            flags.discard(flag)
        return ImageOptions(**{**self.model_dump(), "flags": flags})


class ImagePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_mode: WorkflowMode = WorkflowMode.MANUAL
    image_action: ImageAction | None = (
        None  # automatic rights-aware policy for existing tasks
    )
    image_options: ImageOptions = Field(default_factory=ImageOptions)


PRESETS: dict[ImageAction, frozenset[ImageOption]] = {
    ImageAction.RECREATE: DEFAULT_OPTIONS | {ImageOption.REDESIGN},
    ImageAction.LOCALIZE: DEFAULT_OPTIONS | {ImageOption.SIMILAR_LAYOUT},
    ImageAction.CLEAN_RECREATE: DEFAULT_OPTIONS
    | {ImageOption.REMOVE_OVERLAYS, ImageOption.REDESIGN},
    ImageAction.INFO_CARD: DEFAULT_OPTIONS | {ImageOption.REDESIGN},
    ImageAction.ENHANCE: DEFAULT_OPTIONS
    | {ImageOption.MINIMAL_CHANGES, ImageOption.VISUAL_PRIORITY},
    ImageAction.GENERATE: DEFAULT_OPTIONS | {ImageOption.REDESIGN},
}



def has_image_source(media) -> bool:
    return any((item.get("kind") if isinstance(item, dict) else item.kind)
               in ("photo", "image_document") for item in media)


def resolve_workflow(preferences: ImagePreferences, *, has_images: bool) -> ImagePreferences:
    """Automatic mode creates originals; it never asserts rights to edit source pixels."""
    if preferences.workflow_mode != WorkflowMode.AUTO_BUNDLE:
        return preferences.model_copy(deep=True)
    options = preferences.image_options
    flags = (options.flags - {ImageOption.MINIMAL_CHANGES, ImageOption.SIMILAR_LAYOUT}) | {
        ImageOption.REDESIGN, ImageOption.REMOVE_OVERLAYS,
        ImageOption.KEEP_BRANDS, ImageOption.KEEP_IDENTIFIERS, ImageOption.FACTUAL_PRIORITY,
    }
    return preferences.model_copy(update={
        "image_action": ImageAction.RECREATE if has_images else ImageAction.GENERATE,
        "image_options": ImageOptions(**{**options.model_dump(), "flags": flags,
                                         "promotion_targets": frozenset()}),
    })
