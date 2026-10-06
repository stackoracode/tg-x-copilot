"""Application settings.

Two layers:
1. Bootstrap settings from environment / `.env` (pydantic-settings, nested with `__`).
2. Runtime overrides stored in the MySQL `settings` table, editable from the admin UI.
   Only keys listed in EDITABLE_KEYS can be overridden; DB / Telegram / admin credentials
   are bootstrap-only (chicken-and-egg).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class TelegramSettings(BaseModel):
    enabled: bool = True
    api_id: int = 0
    api_hash: SecretStr = SecretStr("")
    bot_token: SecretStr = SecretStr("")
    session_path: str = "data/bot.session"
    # Only these Telegram user IDs may use the bot. Empty list = nobody (fail closed).
    allowed_user_ids: list[int] = Field(default_factory=list)


class OpenAICompatSettings(BaseModel):
    """OpenAI-compatible endpoint (the CPA proxy) used for text, vision and image models."""

    base_url: str = "http://127.0.0.1:8317/v1"
    api_key: SecretStr = SecretStr("")
    timeout_seconds: float = 90.0
    max_retries: int = 3


class JevSettings(BaseModel):
    """TypeSafe AI System One API (Jev). Native typed questions, NOT a chat endpoint.
    Docs: https://docs.typesafe.ai/api"""

    # Jev is optional: when disabled, unconfigured, slow or failing, triage falls back to the
    # main LLM's evaluation instead of failing the task.
    enabled: bool = True
    base_url: str = "https://api.typesafe.ai/v1"
    api_key: SecretStr = SecretStr("")
    model: str = "jev-latest"
    timeout_seconds: float = 15.0
    max_retries: int = 1
    # Hard wall-clock budget for the whole Jev step (including retries).
    budget_seconds: float = 30.0
    # Routing thresholds (see pipeline/triage.py and docs.typesafe.ai/confidence):
    # below this confidence Jev's value judgment is not trusted and the main LLM decides.
    confidence_floor: float = 0.6
    # normalized value score (0..1) under which a *confident* answer skips the task
    min_value: float = 0.4
    # noul probabilities that trigger skip / human review
    promo_skip: float = 0.8
    risk_review: float = 0.6
    # noul probability under which attached media is not analyzed at all (saves vision calls)
    media_useful_min: float = 0.3


class ModelSettings(BaseModel):
    text_model: str = "gpt-5"
    vision_model: str = "gpt-5"
    image_model: str = "gpt-image-2"
    image_size: str = "1024x1024"
    # "edits": multipart POST /images/edits (OpenAI standard)
    # "generations_with_refs": POST /images/generations with base64 data URLs in `image_urls`
    #   (some CPA relays only accept reference images this way)
    image_edit_mode: Literal["edits", "generations_with_refs"] = "edits"
    # Send response_format={"type":"json_object"}; disable if the upstream model rejects it.
    json_mode: bool = True
    # None = don't send `temperature` (reasoning models such as gpt-5 reject non-default values).
    text_temperature: float | None = None


class R2Settings(BaseModel):
    account_id: str = ""
    access_key_id: SecretStr = SecretStr("")
    secret_access_key: SecretStr = SecretStr("")
    bucket: str = "tg-x-copilot"
    endpoint: str = ""  # override; default https://<account_id>.r2.cloudflarestorage.com
    region: str = "auto"
    public_base_url: str = ""  # optional r2.dev / custom domain for public links
    presign_ttl_seconds: int = 3600
    timeout_seconds: float = 60.0
    max_retries: int = 3
    storage_class: str = "STANDARD"  # R2 Standard storage (free tier applies to Standard only)

    @property
    def endpoint_url(self) -> str:
        if self.endpoint:
            return self.endpoint.rstrip("/")
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


class StorageSettings(BaseModel):
    """R2 usage policy, tuned for the 10 GB free tier.

    Incoming Telegram media is held in memory only while a task runs; nothing is written to R2
    unless the task produces a draft. Then only final assets (kept/enhanced/generated images)
    and, optionally, review copies are uploaded, compressed and content-addressed (sha256).
    Rejecting/regenerating a task deletes its objects unless another task references them.
    """

    # Review copies cost storage for media nobody may use; off by default (the operator still
    # has the originals in the Telegram chat).
    persist_review_media: bool = False
    # When False, approving a task also releases its final assets: they were already delivered
    # to Telegram with the draft, so R2 only holds media for drafts still awaiting a decision.
    retain_approved_assets: bool = False
    # Soft budget: uploads are refused (and the image flagged) once tracked usage exceeds it.
    budget_bytes: int = 9 * 1024**3
    photo_max_side: int = 2048
    graphic_max_side: int = 4096  # screenshots/charts keep more pixels so text stays legible
    jpeg_quality: int = 85


class DBSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "tgx"
    password: SecretStr = SecretStr("")
    database: str = "tg_x_copilot"
    connect_timeout: int = 10
    read_timeout: int = 30


class ConcurrencySettings(BaseModel):
    workers: int = 3
    queue_maxsize: int = 200
    jev: int = 4
    text: int = 2
    vision: int = 2
    image: int = 1
    db: int = 5
    io: int = 4  # Telegram downloads + R2 transfers


class PipelineSettings(BaseModel):
    max_rewrite_attempts: int = 3
    max_media_bytes: int = 20 * 1024 * 1024
    task_timeout_seconds: float = 900.0
    # Telegram chat IDs (e.g. -100123...) whose media the operator owns the rights to.
    # Only media from these sources may be kept or enhanced; everything else is
    # regenerated as an original visual or flagged for review.
    owned_source_ids: list[int] = Field(default_factory=list)
    # Treat media sent directly to the bot (not forwarded) as owned by the operator.
    direct_uploads_owned: bool = False


class AdminSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    username: str = "admin"
    password: SecretStr = SecretStr("")


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_nested_delimiter="__", extra="ignore", env_file_encoding="utf-8"
    )

    log_level: str = "INFO"
    log_json: bool = True
    default_locale: Literal["en-US", "zh-CN"] = "en-US"
    market: str = "US"
    shutdown_grace_seconds: float = 30.0

    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    cpa: OpenAICompatSettings = Field(default_factory=OpenAICompatSettings)
    jev: JevSettings = Field(default_factory=JevSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    r2: R2Settings = Field(default_factory=R2Settings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    db: DBSettings = Field(default_factory=DBSettings)
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)


# Keys editable at runtime from the admin UI (stored in MySQL). API keys and other credentials
# are deliberately absent: they come from the environment only and are never written to MySQL.
EDITABLE_KEYS: frozenset[str] = frozenset({
    "default_locale",
    "market",
    "cpa.base_url",
    "cpa.timeout_seconds",
    "cpa.max_retries",
    "jev.enabled",
    "jev.base_url",
    "jev.model",
    "jev.timeout_seconds",
    "jev.budget_seconds",
    "jev.confidence_floor",
    "jev.min_value",
    "jev.promo_skip",
    "jev.risk_review",
    "jev.media_useful_min",
    "models.text_model",
    "models.vision_model",
    "models.image_model",
    "models.image_size",
    "models.image_edit_mode",
    "models.json_mode",
    "models.text_temperature",
    "r2.account_id",
    "r2.bucket",
    "r2.endpoint",
    "r2.public_base_url",
    "storage.persist_review_media",
    "storage.retain_approved_assets",
    "storage.budget_bytes",
    "storage.photo_max_side",
    "storage.graphic_max_side",
    "storage.jpeg_quality",
    "pipeline.max_rewrite_attempts",
    "pipeline.owned_source_ids",
    "pipeline.direct_uploads_owned",
})


def is_secret_key(key: str) -> bool:
    """True for credential-like settings, which must never be stored in MySQL."""
    leaf = key.rsplit(".", 1)[-1]
    return leaf in {"api_key", "api_hash", "bot_token", "password", "access_key_id",
                    "secret_access_key"}


def get_path(settings: AppSettings, key: str) -> Any:
    node: Any = settings
    for part in key.split("."):
        node = getattr(node, part)
    if isinstance(node, SecretStr):
        return node.get_secret_value()
    return node


def apply_overrides(base: AppSettings, overrides: dict[str, Any]) -> AppSettings:
    """Return a new validated AppSettings with whitelisted overrides applied."""
    data = base.model_dump(mode="python")
    for key, value in overrides.items():
        if key not in EDITABLE_KEYS:
            continue
        node = data
        parts = key.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    # Init kwargs take precedence over env, so this is deterministic.
    return AppSettings(**data)


def secret_values(settings: AppSettings) -> list[str]:
    """All secret strings, for log masking."""
    out: list[str] = []

    def walk(model: BaseModel) -> None:
        for name in type(model).model_fields:
            value = getattr(model, name)
            if isinstance(value, SecretStr):
                out.append(value.get_secret_value())
            elif isinstance(value, BaseModel):
                walk(value)

    walk(settings)
    return [s for s in out if s]
