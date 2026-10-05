"""Domain models shared across layers. Pure Pydantic, no I/O."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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


class ImageDecision(StrEnum):
    KEEP = "keep"
    ENHANCE = "enhance"
    REGENERATE = "regenerate"
    REVIEW = "review"


# ---------------------------------------------------------------- input


class SourceMedia(BaseModel):
    message_id: int
    kind: MediaKind
    mime: str | None = None
    file_name: str | None = None
    size: int | None = None


class ForwardOrigin(BaseModel):
    chat_id: int | None = None  # bot-API style peer id, e.g. -100123...
    sender_name: str | None = None
    post_id: int | None = None
    date: datetime | None = None


class InputEnvelope(BaseModel):
    """Normalized representation of one or more forwarded Telegram messages."""

    chat_id: int
    user_id: int
    message_ids: list[int]
    grouped_ids: list[int] = Field(default_factory=list)
    text: str = ""
    urls: list[str] = Field(default_factory=list)
    media: list[SourceMedia] = Field(default_factory=list)
    forwards: list[ForwardOrigin] = Field(default_factory=list)
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


class ImageAnalysis(BaseModel):
    description: str
    image_type: Literal[
        "photo_real_event", "photo_generic", "chart", "infographic",
        "illustration", "screenshot", "meme", "other",
    ] = "other"
    depicts_real_people: bool = False
    has_third_party_watermark: bool = False
    watermark_text: str | None = None
    contains_text: bool = False
    text_language: str | None = None
    extracted_text: str = ""
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
    names_consistent: bool  # product, brand, organization and place names
    people_consistent: bool  # no people added/removed/altered; no real-person likeness
    watermarks_ok: bool  # no watermark/logo added, and none removed
    facts_consistent: bool  # nothing contradicts the source facts
    rendered_text: str = ""  # all text visible in the candidate, verbatim
    issues: list[str] = Field(default_factory=list)


class MediaResult(BaseModel):
    idx: int
    decision: ImageDecision
    reason: str
    asset_key: str | None = None  # R2 key, only when something was persisted
    asset_kind: Literal["final", "review"] | None = None
    ai_generated: bool = False
