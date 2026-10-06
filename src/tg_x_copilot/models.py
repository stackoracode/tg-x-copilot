"""Domain models shared across layers. Pure Pydantic, no I/O."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .image_settings import ImageAction, ImageOptions, WorkflowMode


class TaskStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    SKIPPED = "skipped"
    DRAFT_READY = "draft_ready"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


TERMINAL = {TaskStatus.APPROVED, TaskStatus.REJECTED}


class MediaKind(StrEnum):
    PHOTO = "photo"
    IMAGE_DOCUMENT = "image_document"
    VIDEO = "video"
    OTHER = "other"
    GENERATED = "generated"


class ImageDecision(StrEnum):
    KEEP = "keep"
    ENHANCE = "enhance"
    REGENERATE = "regenerate"  # backward-compatible stored decision
    LOCALIZE = "localize"
    RECREATE = "recreate"
    CLEAN_RECREATE = "clean_recreate"
    INFO_CARD = "info_card"
    GENERATE = "generate"
    OMIT = "omit"
    TEXT_ONLY = "text_only"
    REVIEW = "review"


# ---------------------------------------------------------------- input


class SourceMedia(BaseModel):
    message_id: int
    kind: MediaKind
    forwarded: bool | None = None  # None for legacy envelopes
    source_chat_id: int | None = None
    mime: str | None = None
    file_name: str | None = None
    size: int | None = None


class ForwardOrigin(BaseModel):
    chat_id: int | None = None  # bot-API style peer id, e.g. -100123...
    sender_name: str | None = None
    post_id: int | None = None
    date: datetime | None = None


class ImageEditAuthorization(BaseModel):
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    image_idx: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    user_id: int
    confirmed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InputEnvelope(BaseModel):
    """Normalized representation of one or more forwarded Telegram messages."""

    chat_id: int
    user_id: int
    message_ids: list[int]
    grouped_ids: list[int] = Field(default_factory=list)
    text: str = ""
    message_texts: list[str] = Field(default_factory=list)  # one caption per message
    message_urls: list[list[str]] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    media: list[SourceMedia] = Field(default_factory=list)
    forwards: list[ForwardOrigin] = Field(default_factory=list)
    workflow_mode: WorkflowMode = WorkflowMode.MANUAL
    image_action: ImageAction | None = None
    image_options: ImageOptions = Field(default_factory=ImageOptions)
    processing_mode: Literal["full", "images_only"] = "full"
    image_retry_indices: list[int] | None = None
    image_edit_authorizations: dict[str, ImageEditAuthorization] = Field(default_factory=dict)
    locale: str = "en-US"
    market: str = "US"
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def source_chat_ids(self) -> set[int]:
        return {f.chat_id for f in self.forwards if f.chat_id is not None}


# ---------------------------------------------------------------- model outputs


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


class TriageResult(BaseModel):
    """Routing decision derived from Jev's typed answers (see pipeline/triage.py)."""

    route: Literal["skip", "proceed", "review"]
    value: float  # Jev score question normalized to 0..1
    confidence: float | None = None  # Jev confidence of the value score
    content_type: str | None = None
    content_type_confidence: float | None = None
    flags: dict[str, float] = Field(default_factory=dict)  # noul probabilities
    use_media: bool = True
    escalated: bool = False  # Jev was not confident; the main LLM's evaluation decides
    reasons: list[str] = Field(default_factory=list)
    model: str = ""
    usage: dict[str, int] = Field(default_factory=dict)

    @property
    def summary(self) -> str:
        conf = f"{self.confidence:.2f}" if self.confidence is not None else "n/a"
        flags = ", ".join(f"{k}={v:.2f}" for k, v in self.flags.items())
        return (f"route={self.route} value={self.value:.2f} confidence={conf} "
                f"type={self.content_type} {flags}"
                + (" (escalated: Jev uncertain)" if self.escalated else ""))


class MediaFailureStage(StrEnum):
    IMAGE_POLICY = "IMAGE_POLICY"
    IMAGE2 = "IMAGE2"
    QC = "QC"
    R2_UPLOAD = "R2_UPLOAD"
    R2_FETCH = "R2_FETCH"
    TELEGRAM_SEND = "TELEGRAM_SEND"


class MarkRegion(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,16}$")
    text: str = ""
    kind: Literal["promotion", "copyright", "author", "photographer", "media_rights", "unknown"]
    # Normalized display coordinates (EXIF-corrected image), not publishing facts.
    box: tuple[float, float, float, float]
    confidence: float = Field(default=0, ge=0, le=1)
    safe_to_remove: bool = False  # simple background is OK; no meaningful source content
    removal_risk: Literal["background_only", "content_occluded", "protected", "uncertain"] | None = None
    removal_reason: str = ""

    @model_validator(mode="after")
    def valid_box(self):
        left, top, right, bottom = self.box
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("invalid mark region coordinates")
        return self


class ImageAnalysis(BaseModel):
    description: str
    layout_description: str = ""  # information hierarchy, never factual evidence
    image_type: Literal[
        "photo_real_event", "photo_generic", "chart", "infographic",
        "illustration", "screenshot", "meme", "other",
        "ui_screenshot", "mixed_layout", "generic_visual", "brand_asset", "text_input",
    ] = "other"
    depicts_real_people: bool = False
    has_third_party_watermark: bool = False
    watermark_text: str | None = None
    has_channel_overlay: bool = False
    has_source_copyright_mark: bool | None = None  # unknown for old analysis
    mark_regions: list[MarkRegion] = Field(default_factory=list)
    brand_names: list[str] = Field(default_factory=list)
    source_facts: list[str] = Field(default_factory=list)
    contains_text: bool = False
    text_language: str | None = None
    text_script: str | None = None  # simplified / traditional / mixed / other
    extracted_text: str = ""
    content_text: str | None = None  # derived publishing OCR; raw OCR remains intact for QC
    quality: Literal["low", "ok", "high"] = "ok"
    relevance: float = 0.5
    sensitive: bool = False
    reason: str = ""

    @field_validator("relevance")
    @classmethod
    def clamp_relevance(cls, v: float) -> float:
        return _clamp01(v)


class Evaluation(BaseModel):
    suitable: bool
    value_score: float
    audience: str = ""
    angle: str = ""
    key_facts: list[str] = Field(default_factory=list)
    background_points: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("value_score")
    @classmethod
    def clamp_value_score(cls, v: float) -> float:
        return _clamp01(v)


class Claim(BaseModel):
    text: str
    # source     = stated in the source
    # background = factual context added by the LLM (unverified -> requires human review)
    # opinion    = explanation, analysis or opinion that asserts no new fact
    basis: Literal["source", "background", "opinion"]


class RewriteResult(BaseModel):
    post: str
    hook: str
    claims: list[Claim] = Field(default_factory=list)
    is_mere_translation: bool = False
    added_value: str = ""
    image_brief: str = ""  # used when regenerating an original visual


class ImageQC(BaseModel):
    """Vision-model verification of an Image2 output (enhance / localize / regenerate)."""

    passed: bool
    text_consistent: bool  # important text preserved (enhance), faithfully translated
    #                        (localize), or correctly spelled and allowed (regenerate)
    numbers_consistent: bool
    dates_consistent: bool
    brands_consistent: bool = False  # logos/brands verified against source
    names_consistent: bool  # product, brand, organization and place names
    people_consistent: bool  # no people added/removed/altered; no real-person likeness
    watermarks_ok: bool  # no unauthorized removal; explicitly permitted promotion removal is OK
    protected_marks_preserved: bool = False
    promotion_removal_valid: bool = False
    outside_regions_unchanged: bool = False
    facts_consistent: bool  # nothing contradicts the source facts
    identifiers_consistent: bool = False
    readability_ok: bool = False
    density_consistent: bool = False
    language_consistent: bool = False  # fail closed when upstream omits this check
    rendered_text: str = ""  # all text visible in the candidate, verbatim
    issues: list[str] = Field(default_factory=list)


class MediaResult(BaseModel):
    idx: int
    decision: ImageDecision | ImageAction
    reason: str
    failure_stage: MediaFailureStage | None = None
    asset_key: str | None = None  # R2 key, only when something was persisted
    asset_kind: Literal["final", "review"] | None = None
    ai_generated: bool = False


class VerifiedFact(BaseModel):
    text: str
    evidence: str  # exact span in cleaned core content or source OCR
    source_idx: int | None = None  # None = cleaned core; otherwise source image index


class VerifiedFacts(BaseModel):
    facts: list[VerifiedFact] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(f.text for f in self.facts)


class FactVerification(BaseModel):
    passed: bool
