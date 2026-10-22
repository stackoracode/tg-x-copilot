"""Quick create: any input -> one ready post + one generated image. Telegram and web.

A separate, deliberately short path for operators who always want text *and* a picture:

    links (optional) -> load attached images -> one model call writes the post and an image
    prompt (it sees the attached images) -> Image2 (attached image as reference, else
    text-to-image; one retry, then plain generation) -> R2 -> draft ready -> delivery

Skipped on purpose: Jev triage, suitability evaluation, fact guards, the rights-aware image
policy, edit-rights confirmation and image QC. Who may submit is unchanged (Telegram allow
list / admin login). Attached images are used only as references and are not delivered.
If the image still fails, the post is delivered with the failure (status needs_review).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .. import prompts
from ..clients.openai_compat import image_data_url
from ..config import AppSettings
from ..i18n import Locale, t
from ..image_settings import ImageAction
from ..logging_setup import ctx as log_ctx
from ..logging_setup import mask
from ..models import ImageDecision, InputEnvelope, MediaFailureStage, TaskStatus
from .guards import x_length
from .links import attach_linked_pages
from .media_errors import media_error_reason

if TYPE_CHECKING:
    from .processor import ImageOutcome, LoadedImage, Pipeline

log = logging.getLogger(__name__)

_COMMAND = re.compile(r"^\s*/quick(?:@\w+)?\b\s*", re.I)
_MAX_REFERENCE_IMAGES = 4  # images shown to the writing model
IMAGE_ATTEMPTS = 2


class QuickDraft(BaseModel):
    post: str
    image_prompt: str
    alt_text: str = ""


def strip_command(env: InputEnvelope) -> InputEnvelope:
    """Drop a leading `/quick` (Telegram one-shot command) from the source text."""
    texts = [_COMMAND.sub("", text, count=1) for text in env.message_texts]
    return env.model_copy(update={"text": _COMMAND.sub("", env.text, count=1).strip(),
                                  "message_texts": texts})


class QuickCreate:
    def __init__(self, pipeline: "Pipeline") -> None:
        self.p = pipeline
        self.app = pipeline.app

    # ------------------------------------------------------------------ full run

    async def run(self, task_id: str, task: dict[str, Any], env: InputEnvelope, locale: Locale,
                  cfg: AppSettings) -> None:
        p, repo = self.p, self.app.repo
        started = time.monotonic()
        if task["attempts"]:  # leftovers from an interrupted earlier run
            try:
                await self.app.storage.release_task(task_id)
            except Exception:
                log.exception("releasing leftover assets failed (non-fatal)")
        env = strip_command(env)
        await p._event(task_id, "quick", f"quick create, attempt {task['attempts'] + 1}",
                       data={"media": len(env.media), "urls": env.urls})
        await repo.set_stage(task_id, "links")
        env, link_notes, _ = await attach_linked_pages(task_id, env, self.app, p._event)
        await repo.set_stage(task_id, "media")
        images, dup_warnings = await p._load_media(task_id, env, cfg, use=True)
        try:
            await repo.set_stage(task_id, "rewrite")
            draft = await self._write(task_id, env, images, locale, cfg)
            await repo.set_stage(task_id, "images")
            outcomes = self._reference_outcomes(env, images)
            outcomes.append(await self._image(task_id, env, draft.image_prompt, images, cfg))
            await repo.set_stage(task_id, "persist")
            await p._ensure_media_rows(task_id, env, images, outcomes, {})
            results = await p._persist(task_id, images, outcomes, {}, cfg, locale=env.locale)
        finally:
            images.clear()
        generated = results[-1]
        image_ok = generated.asset_kind == "final"
        warnings = dup_warnings + link_notes
        length = x_length(draft.post)
        if cfg.pipeline.max_post_chars and length > cfg.pipeline.max_post_chars:
            warnings.append(t(env.locale, "quick_too_long", length=length,
                              limit=cfg.pipeline.max_post_chars))
        review = [] if image_ok else [t(env.locale, "quick_image_missing")]
        meta = {
            "quick": True,
            "canonical_text": env.text,
            "image_prompt": draft.image_prompt,
            "alt_text": draft.alt_text,
            "image_action": ImageAction.GENERATE.value,
            "image_options": env.image_options.model_dump(mode="json"),
            "image_locale": env.image_options.target_locale or env.locale,
            "hook": "", "claims": [], "added_value": "",
            "problems": [],
            "review": review, "text_review": [],
            "warnings": warnings, "text_warnings": warnings,
            "risks": [],
            "media": [r.model_dump(mode="json") for r in results],
            "x_length": length,
            "elapsed_s": round(time.monotonic() - started, 1),
        }
        status = TaskStatus.DRAFT_READY if image_ok else TaskStatus.NEEDS_REVIEW
        if not await p._save_result(task_id, status, draft.post, meta):
            return
        await p._event(task_id, "done", f"quick {status.value} in {meta['elapsed_s']}s")
        await p._notify_draft(task_id)

    # ------------------------------------------------------------------ image-only re-run

    async def images_only(self, task_id: str, task: dict[str, Any], env: InputEnvelope,
                          locale: Locale, cfg: AppSettings) -> None:
        """'New image' for a quick task: same post, same image prompt, fresh image."""
        p = self.p
        meta = dict(task.get("draft_meta") or {})
        if not task.get("draft_text"):
            raise ValueError("quick task has no draft")
        prompt = meta.get("image_prompt") or task["draft_text"]
        images, warnings = await p._load_media(task_id, env, cfg, use=True)
        try:
            outcomes = self._reference_outcomes(env, images)
            outcomes.append(await self._image(task_id, env, prompt, images, cfg))
            await self.app.storage.release_task(task_id)
            await self.app.repo.delete_media_rows(task_id)
            await p._ensure_media_rows(task_id, env, images, outcomes, {})
            results = await p._persist(task_id, images, outcomes, {}, cfg, locale=env.locale)
        finally:
            images.clear()
        image_ok = results[-1].asset_kind == "final"
        meta.update(media=[r.model_dump(mode="json") for r in results],
                    review=[] if image_ok else [t(env.locale, "quick_image_missing")],
                    warnings=list(meta.get("text_warnings") or []) + warnings, delivery={})
        status = TaskStatus.DRAFT_READY if image_ok else TaskStatus.NEEDS_REVIEW
        if not await p._save_result(task_id, status, task["draft_text"], meta):
            return
        await p._event(task_id, "images", "quick image re-run finished; post unchanged")
        await p._notify_draft(task_id)

    # ------------------------------------------------------------------ steps

    def _reference_outcomes(self, env: InputEnvelope,
                            images: list["LoadedImage"]) -> list["ImageOutcome"]:
        from .processor import ImageOutcome
        loaded = {image.idx for image in images}
        return [ImageOutcome(idx, ImageDecision.OMIT,
                             t(env.locale, "quick_reference" if idx in loaded else "quick_unused"))
                for idx in range(len(env.media))]

    async def _write(self, task_id: str, env: InputEnvelope, images: list["LoadedImage"],
                     locale: Locale, cfg: AppSettings) -> QuickDraft:
        image_locale = self.app.i18n.get(env.image_options.target_locale or locale.code)
        refs = images[:_MAX_REFERENCE_IMAGES]
        p = prompts.render(
            "quick_compose", locale.code, market=env.market, language_name=locale.language_name,
            image_language_name=image_locale.language_name,
            max_chars=cfg.pipeline.max_post_chars or 280,
            instructions=env.quick_instructions.strip() or t(locale.code, "none"),
            text=env.text[:12000] or t(locale.code, "empty_text"), image_count=len(refs),
            density=env.image_options.information_density.value)
        content: Any = p.user
        if refs:
            content = [{"type": "text", "text": p.user}] + [
                {"type": "image_url", "image_url": {"url": image_data_url(i.blob.data, i.blob.mime)}}
                for i in refs]
        messages = [{"role": "system", "content": p.system}, {"role": "user", "content": content}]
        model = cfg.models.vision_model if refs else cfg.models.text_model
        limit = cfg.pipeline.max_post_chars
        best: QuickDraft | None = None
        for attempt in range(1, max(1, cfg.pipeline.max_rewrite_attempts) + 1):
            async with (self.app.limits.vision if refs else self.app.limits.text):
                draft = await self.app.hub.cpa.chat_json(
                    model, messages, QuickDraft, json_mode=cfg.models.json_mode,
                    temperature=cfg.models.text_temperature)
            draft = QuickDraft(post=draft.post.strip(), image_prompt=draft.image_prompt.strip(),
                               alt_text=draft.alt_text.strip())
            if not draft.post or not draft.image_prompt:
                continue
            if best is None or x_length(draft.post) < x_length(best.post):
                best = draft
            if not limit or x_length(draft.post) <= limit:
                break
            await self.p._event(task_id, "quick", f"attempt {attempt}: post too long "
                                f"({x_length(draft.post)}/{limit}); shortening", level="warning")
            messages = messages + [
                {"role": "assistant", "content": draft.model_dump_json()},
                {"role": "user", "content": t(locale.code, "quick_shorten", limit=limit)}]
        if best is None:
            raise ValueError("quick create: the model returned no post / image prompt")
        await self.p._event(task_id, "quick", "post and image prompt written",
                            data={"post": best.post, "image_prompt": best.image_prompt})
        return best

    async def _image(self, task_id: str, env: InputEnvelope, image_prompt: str,
                     images: list["LoadedImage"], cfg: AppSettings) -> "ImageOutcome":
        from .processor import ImageOutcome
        m, hub = cfg.models, self.app.hub
        idx = len(env.media)
        reference = images[0] if images else None
        image_locale = self.app.i18n.get(env.image_options.target_locale or env.locale)
        prompt = prompts.render(
            "quick_image", image_locale.code, image_prompt=image_prompt,
            language_name=image_locale.language_name, market=env.market,
            reference=t(image_locale.code, "quick_with_reference") if reference else "",
            density=env.image_options.information_density.value).user
        reason = t(env.locale, "quick_generated")
        error: BaseException | None = None
        # Attempt 1 (and 2) use the attached image as reference; if the edit endpoint keeps
        # failing, the last attempt falls back to text-to-image so a picture still arrives.
        plan = ["edit"] * IMAGE_ATTEMPTS + ["generate"] if reference else ["generate"] * IMAGE_ATTEMPTS
        for attempt, mode in enumerate(plan, 1):
            try:
                async with self.app.limits.image:
                    if mode == "edit":
                        raw = await hub.cpa.images_edit(m.image_model, prompt, reference.blob.data,
                                                        reference.blob.mime, size=m.image_size,
                                                        mode=m.image_edit_mode)
                    else:
                        raw = await hub.cpa.images_generate(m.image_model, prompt, size=m.image_size)
                output = await asyncio.to_thread(self.p._optimize, raw, True, cfg)
                await self.p._event(task_id, "quick_image", f"image produced ({mode}, attempt {attempt})")
                return ImageOutcome(idx, ImageDecision.GENERATE, reason, output, ai_generated=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = exc
                log.warning("quick image attempt failed", extra=log_ctx(attempt=attempt, mode=mode))
                await self.p._event(task_id, "quick_image",
                                    mask(f"attempt {attempt} ({mode}) failed: "
                                         f"{type(exc).__name__}: {exc}")[:300], level="warning")
        return ImageOutcome(idx, ImageDecision.REVIEW,
                            t(env.locale, "media_stage_image2") + " "
                            + media_error_reason(error, env.locale),
                            failure_stage=MediaFailureStage.IMAGE2)
