"""The processing pipeline for one task.

triage (Jev, text only) -> [skip] | download media into memory -> vision -> LLM evaluation ->
rewrite (+guards, retry with feedback) -> image actions (in memory) -> persist only the assets
the draft needs to R2 -> draft (ready / needs review) -> notify.

Storage rules: incoming media never touches disk or R2 unless the task yields a draft; skipped,
unsuitable and failed tasks leave nothing behind. Each step records a task_event so the admin
UI shows a full timeline. Per-image work is isolated: one failing image becomes REVIEW.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import prompts
from ..clients.openai_compat import image_data_url
from ..config import AppSettings
from ..i18n import Locale, label, t
from ..image_settings import ACTIONS, ImageAction, ImageOption
from ..logging_setup import ctx as log_ctx
from ..logging_setup import mask
from ..models import (
    Evaluation, ImageAnalysis, ImageDecision, ImageQC, InputEnvelope, MediaKind,
    MediaResult, RewriteResult, SourceMedia, TaskStatus, TriageResult, VerifiedFacts,
)
from ..services.storage import StorageBudgetExceeded
from .cleaning import clean_bundle
from .guards import GuardReport, XRules, check_rewrite, x_length
from .language import is_localized
from .image_policy import plan, media_is_owned
from .facts import build_verified_facts, packet_is_traceable
from .image_qc import QCMode, build_messages, qc_verdict
from .media import ImageBlob, graphic_hint, inspect_image, optimize_image
from .triage import build_questions, build_state, decide_route, has_usable_text, without_jev

if TYPE_CHECKING:
    from ..app_context import AppContext

log = logging.getLogger(__name__)

_IMAGE_KINDS = {MediaKind.PHOTO, MediaKind.IMAGE_DOCUMENT}


@dataclass
class LoadedImage:
    """Incoming image held in memory for the duration of the task only."""
    idx: int
    blob: ImageBlob
    source: SourceMedia


@dataclass
class ImageOutcome:
    idx: int
    decision: ImageDecision | ImageAction
    reason: str
    output: ImageBlob | None = None  # optimized final asset (keep/enhance/regenerate)
    ai_generated: bool = False
    review_blob: ImageBlob | None = None  # e.g. an Image2 output that failed QC


class Pipeline:
    def __init__(self, app: "AppContext") -> None:
        self.app = app

    # ------------------------------------------------------------------ helpers

    async def _event(self, task_id: str, step: str, message: str, *, level: str = "info",
                     data: Any = None) -> None:
        log.log(logging.getLevelName(level.upper()), f"[{step}] {message}")
        try:
            await self.app.repo.add_event(task_id, step, mask(message), level=level, data=data)
        except Exception:
            log.exception("failed to write task event")

    @staticmethod
    def _source_info(env: InputEnvelope) -> str:
        # Attribution is rights/audit metadata, never editorial content or Jev input.
        return ""

    @staticmethod
    def _base_vars(env: InputEnvelope, locale: Locale) -> dict[str, Any]:
        return {"market": env.market, "language_name": locale.language_name}

    def _optimize(self, data: bytes, graphic: bool | None, cfg: AppSettings) -> ImageBlob:
        s = cfg.storage
        return optimize_image(data, graphic=graphic, photo_max_side=s.photo_max_side,
                              graphic_max_side=s.graphic_max_side, jpeg_quality=s.jpeg_quality)

    # ------------------------------------------------------------------ entry points

    async def process(self, task_id: str) -> None:
        repo = self.app.repo
        task = await repo.get_task(task_id)
        if task is None:
            log.warning("task not found")
            return
        # Atomic claim: only one worker/process can move RECEIVED -> PROCESSING.
        if not await repo.claim_task(task_id, self.app.instance_id):
            log.info("task not claimed (already taken or not runnable)",
                     extra=log_ctx(status=task["status"]))
            return
        try:
            await self._process_claimed(task_id, task)
        except asyncio.CancelledError:
            # Graceful shutdown: hand the task back so the next start picks it up immediately.
            # A per-task timeout also cancels us, but then the worker marks the task FAILED,
            # so the claim must NOT be released (the sweeper could otherwise re-run it).
            if self.app.shutting_down:
                try:
                    await asyncio.shield(repo.release_claim(task_id, self.app.instance_id))
                except Exception:
                    log.exception("could not release claim; it will be requeued after the lease")
            raise

    async def _process_claimed(self, task_id: str, task: dict[str, Any]) -> None:
        repo = self.app.repo
        cfg = self.app.config.current
        env = InputEnvelope.model_validate(task["envelope"])
        locale = self.app.i18n.get(env.locale)
        started = time.monotonic()
        if env.processing_mode == "images_only":
            await self._process_images_only(task_id, task, env, locale, cfg)
            return
        if task["attempts"]:  # leftovers from an interrupted earlier run
            try:
                await self.app.storage.release_task(task_id)
            except Exception:
                log.exception("releasing leftover assets failed (non-fatal)")
        await self._event(task_id, "start", f"attempt {task['attempts'] + 1}",
                          data={"messages": len(env.message_ids), "media": len(env.media)})

        # Clean each message before Jev; keep original envelope/source text for audit.
        await repo.set_stage(task_id, "clean")
        env, clean_fallback = await clean_bundle(env, self.app)
        await self._event(task_id, "clean", "canonical content prepared",
                          data={"text": env.text, "urls": env.urls})
        # 1. Jev sees only core content, before any media download.
        await repo.set_stage(task_id, "triage")
        triage = await self._triage(task_id, env, locale)
        await repo.save_triage(task_id, triage)
        await self._event(task_id, "triage", f"{triage.summary}; {' '.join(triage.reasons)}",
                          data=triage.model_dump())
        if triage.route == "skip":
            await self._skip(task_id, env, "Jev: " + " ".join(triage.reasons))
            return

        # 2. Media, in memory only.
        await repo.set_stage(task_id, "media")
        images, dup_warnings = await self._load_media(task_id, env, cfg, use=triage.use_media)
        try:
            await self._run_editorial(task_id, env, locale, cfg, triage, images, dup_warnings,
                                      started, clean_fallback=clean_fallback)
        finally:
            images.clear()  # drop references to downloaded bytes promptly

    async def _run_editorial(self, task_id: str, env: InputEnvelope, locale: Locale,
                             cfg: AppSettings, triage: TriageResult, images: list[LoadedImage],
                             dup_warnings: list[str], started: float, *,
                             clean_fallback: bool = False) -> None:
        repo = self.app.repo
        await repo.set_stage(task_id, "vision")
        analyses = await self._analyze(task_id, env, images, locale, cfg)

        await repo.set_stage(task_id, "evaluate")
        evaluation = await self._evaluate(env, triage, analyses, locale, cfg)
        await repo.save_evaluation(task_id, evaluation)
        await self._event(task_id, "evaluate",
                          f"suitable={evaluation.suitable} value={evaluation.value_score:.2f}: "
                          f"{evaluation.reason}", data=evaluation.model_dump())
        if not evaluation.suitable:
            await self._skip(task_id, env, evaluation.reason)
            return

        await repo.set_stage(task_id, "rewrite")
        knowledge = prompts.render_json("knowledge", locale.code)
        rules = XRules.from_db({**knowledge["rules"],
                               **await repo.get_rules(env.locale, env.market)})
        hooks = await repo.get_hooks(env.locale, env.market) or knowledge["hooks"]
        rewrite, report = await self._rewrite(task_id, env, evaluation, analyses, rules, hooks,
                                              locale, cfg)

        action_spec = ACTIONS.get(env.image_action)
        needs_packet = (bool(env.media) and (action_spec is None or
                        action_spec.execution in ("edit", "create"))) or bool(
                        action_spec and action_spec.text_capable)
        packet, fact_warnings = (await self._fact_packet(task_id, env, analyses)
                                 if needs_packet else (VerifiedFacts(), []))
        await repo.set_stage(task_id, "images")
        usable = [img for img in images if img.idx in analyses]
        for img in usable[rules.max_images:]:
            await repo.update_media_decision(task_id, img.idx, ImageDecision.REVIEW.value,
                                              t(env.locale, "media_limit", count=rules.max_images))
        outcomes = await self._process_images(
            task_id, env, usable[: rules.max_images], analyses, rewrite, evaluation, locale, cfg,
            verified_facts=packet, max_images=rules.max_images
        )

        # 3. Only now, with a draft in hand, persist what the operator needs.
        await repo.set_stage(task_id, "persist")
        await self._ensure_media_rows(task_id, env, images, outcomes, analyses)
        media_results = await self._persist(task_id, images, outcomes, analyses, cfg, locale=locale.code)

        problems = list(report.problems)  # blocking: cannot be approved
        review = list(report.review)  # must be checked by a human, then may be approved
        if triage.route == "review":
            review.append(t(env.locale, "jev_review", reason=" ".join(triage.reasons)))
        text_review = list(review)
        media_review = [r for r in media_results if r.decision == ImageDecision.REVIEW]
        if media_review:
            review.append(t(env.locale, "images_review", count=len(media_review)))
        status = (TaskStatus.DRAFT_READY if not problems and not review
                  else TaskStatus.NEEDS_REVIEW)
        meta = {
            "verified_facts": packet.model_dump(mode="json"),
            "canonical_text": env.text,
            "source_analyses": {str(idx): a.model_dump(mode="json") for idx, a in analyses.items()},
            "image_action": env.image_action.value if env.image_action else None,
            "image_options": env.image_options.model_dump(mode="json"),
            "image_locale": env.image_options.target_locale or env.locale,
            "text_review": text_review,
            "text_warnings": report.warnings + dup_warnings,
            "hook": rewrite.hook,
            "claims": [c.model_dump() for c in rewrite.claims],
            "added_value": rewrite.added_value,
            "problems": problems,
            "review": review,
            "warnings": report.warnings + dup_warnings + fact_warnings +
                        ([t(env.locale, "clean_failed")] if clean_fallback else []),
            "risks": evaluation.risks,
            "media": [r.model_dump(mode="json") for r in media_results],
            "x_length": x_length(rewrite.post),
            "elapsed_s": round(time.monotonic() - started, 1),
        }
        await repo.save_draft(task_id, status, rewrite.post, meta)
        await self._event(task_id, "done", f"{status.value} in {meta['elapsed_s']}s",
                          data={"problems": problems, "review": review})
        await self._notify_draft(task_id)

    async def mark_failed(self, task_id: str, exc: BaseException) -> None:
        error = mask(f"{type(exc).__name__}: {exc}")[:1000]
        try:
            previous = await self.app.repo.get_task(task_id)
            if previous and (previous.get("envelope") or {}).get("processing_mode") == "images_only" and previous.get("draft_text"):
                meta = dict(previous.get("draft_meta") or {})
                meta["review"] = list(meta.get("review") or []) + [t(previous["locale"], "image_rerun_failed")]
                await self.app.repo.save_draft(task_id, TaskStatus.NEEDS_REVIEW, previous["draft_text"], meta)
                await self._event(task_id, "failed", error, level="error")
                await self._notify_draft(task_id)
                return
            await self.app.storage.release_task(task_id)  # nothing persisted for failed tasks
            await self.app.repo.set_status(task_id, TaskStatus.FAILED, stage="failed", error=error)
            await self._event(task_id, "failed", error, level="error")
            task = await self.app.repo.get_task(task_id)
            if task and self.app.telegram:
                await self.app.telegram.notify(
                    task["tg_chat_id"],
                    self.app.i18n.t(task["locale"], "failed", task_id=task_id,
                                    error=t(task["locale"], "task_failure")),
                )
        except Exception:
            log.exception("failed to record task failure")

    async def _skip(self, task_id: str, env: InputEnvelope, reason: str) -> None:
        await self.app.repo.set_status(task_id, TaskStatus.SKIPPED, stage="skipped",
                                       error=reason[:1000])
        await self._event(task_id, "skipped", reason)
        if self.app.telegram:
            try:
                await self.app.telegram.notify(
                    env.chat_id, self.app.i18n.t(env.locale, "skipped", task_id=task_id,
                                                 reason=html.escape(reason[:300], quote=False)))
            except Exception:
                log.exception("notify skipped failed")

    async def _notify_draft(self, task_id: str) -> None:
        if not self.app.telegram:
            return
        try:
            await self.app.telegram.send_draft(task_id)
        except Exception:
            log.exception("sending draft to Telegram failed")
            await self._event(task_id, "notify", "sending draft to Telegram failed", level="warning")

    # ------------------------------------------------------------------ steps

    async def _triage(self, task_id: str, env: InputEnvelope, locale: Locale) -> TriageResult:
        """Jev triage, optional at runtime: disabled / unconfigured / timeout / error all fall
        back to the main LLM's evaluation instead of failing the task."""
        has_media = bool(env.media)
        cfg = self.app.config.current.jev
        if not cfg.enabled:
            return without_jev(t(env.locale, "jev_disabled"), has_media=has_media)
        if not self.app.hub.jev.configured:
            return without_jev(t(env.locale, "jev_unconfigured"),
                               has_media=has_media)
        if not has_usable_text(env):
            return without_jev(t(env.locale, "jev_no_text"),
                               has_media=has_media)
        state = build_state(env, self._source_info(env))
        questions = build_questions(env, locale.code)
        try:
            async with self.app.limits.jev:
                resp = await asyncio.wait_for(self.app.hub.jev.ask(state, questions),
                                              timeout=cfg.budget_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                detail = f"timed out after {cfg.budget_seconds:.0f}s"
            else:
                detail = f"{type(exc).__name__}: {exc}"
            reason = mask(f"Jev unavailable ({detail})")[:300]
            log.warning("jev unavailable; falling back to main LLM", extra=log_ctx(error=reason))
            await self._event(task_id, "triage", reason + "; falling back to main LLM",
                              level="warning")
            return without_jev(t(env.locale, "jev_unavailable"), has_media=has_media)
        return decide_route(resp, cfg, has_text=True, has_media=has_media, locale=env.locale)

    async def _load_media(self, task_id: str, env: InputEnvelope, cfg: AppSettings, *,
                          use: bool) -> tuple[list[LoadedImage], list[str]]:
        repo = self.app.repo
        max_bytes = cfg.pipeline.max_media_bytes

        async def review(idx: int, sm: SourceMedia, reason: str) -> None:
            await repo.upsert_media(task_id, idx, message_id=sm.message_id, kind=sm.kind.value,
                                    mime=sm.mime, size_bytes=sm.size)
            await repo.update_media_decision(task_id, idx, ImageDecision.REVIEW.value, reason)

        wanted = [
            sm.message_id for sm in env.media
            if use and sm.kind in _IMAGE_KINDS and (sm.size or 0) <= max_bytes
        ]
        downloaded: dict[int, bytes] = {}
        download_error = t(env.locale, "media_deleted")
        if wanted and self.app.telegram:
            try:
                async with self.app.limits.io:
                    downloaded = await self.app.telegram.fetch_media(env.chat_id, wanted)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # media is never fatal: the text draft still goes out
                download_error = mask(f"Media download failed ({type(exc).__name__}: {exc}); "
                                      "continuing with text only.")[:300]
                await self._event(task_id, "media", download_error, level="warning")
                download_error = t(env.locale, "media_download")

        loaded: list[LoadedImage] = []
        warnings: list[str] = []
        for idx, sm in enumerate(env.media):
            if not use:
                await review(idx, sm, t(env.locale, "media_unused"))
                continue
            if sm.kind not in _IMAGE_KINDS:
                await review(idx, sm, t(env.locale, "media_unsupported"))
                continue
            if (sm.size or 0) > max_bytes:
                await review(idx, sm, t(env.locale, "media_large", size=sm.size))
                continue
            data = downloaded.pop(sm.message_id, None)
            if data is None:
                await review(idx, sm, download_error)
                continue
            blob = await asyncio.to_thread(inspect_image, data)
            if blob is None:
                await review(idx, sm, t(env.locale, "media_unreadable"))
                continue
            await repo.upsert_media(task_id, idx, message_id=sm.message_id, kind=sm.kind.value,
                                    mime=blob.mime, size_bytes=blob.size, width=blob.width,
                                    height=blob.height, source_sha256=blob.sha256)
            for prev in await repo.previous_uses(blob.sha256, task_id):
                warnings.append(t(env.locale, "media_duplicate", idx=idx, task_id=prev["task_id"][:8],
                                  status=label(env.locale, prev["status"])))
                break
            loaded.append(LoadedImage(idx, blob, sm))

        await self._event(task_id, "media",
                          f"{len(loaded)}/{len(env.media)} image(s) loaded in memory (not stored)")
        return loaded, warnings

    async def _analyze(self, task_id: str, env: InputEnvelope, images: list[LoadedImage],
                       locale: Locale, cfg: AppSettings) -> dict[int, ImageAnalysis]:
        if not images:
            return {}

        async def one(img: LoadedImage) -> tuple[int, ImageAnalysis]:
            p = prompts.render("vision_analyze", locale.code, **self._base_vars(env, locale),
                               text=env.text[:3000] or t(locale.code, "empty_text"))
            messages = [
                {"role": "system", "content": p.system},
                {"role": "user", "content": [
                    {"type": "text", "text": p.user},
                    {"type": "image_url",
                     "image_url": {"url": image_data_url(img.blob.data, img.blob.mime)}},
                ]},
            ]
            async with self.app.limits.vision:
                analysis = await self.app.hub.cpa.chat_json(
                    cfg.models.vision_model, messages, ImageAnalysis,
                    json_mode=cfg.models.json_mode,
                )
            if not await is_localized([analysis.description, analysis.layout_description, analysis.reason,
                                       *analysis.source_facts], locale, self.app):
                raise ValueError("vision narrative has wrong language")
            await self.app.repo.update_media_analysis(task_id, img.idx, analysis)
            return img.idx, analysis

        results = await asyncio.gather(*(one(i) for i in images), return_exceptions=True)
        analyses: dict[int, ImageAnalysis] = {}
        for img, res in zip(images, results):
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, BaseException):
                reason = mask(f"Vision analysis failed: {res}")[:500]
                await self._event(task_id, "vision", f"image {img.idx}: {reason}", level="warning")
                await self.app.repo.update_media_decision(
                    task_id, img.idx, ImageDecision.REVIEW.value, t(env.locale, "vision_failed"))
            else:
                analyses[res[0]] = res[1]
        await self._event(task_id, "vision", f"analyzed {len(analyses)}/{len(images)} image(s)",
                          data={i: a.image_type for i, a in analyses.items()})
        return analyses

    async def _evaluate(self, env: InputEnvelope, triage: TriageResult,
                        analyses: dict[int, ImageAnalysis], locale: Locale,
                        cfg: AppSettings) -> Evaluation:
        notes = "\n".join(
            f"- Image {i}: {a.image_type}; {a.description}"
            + (f"; text ({a.text_language}): {a.extracted_text[:8000]}" if a.contains_text else "")
            for i, a in sorted(analyses.items())
        ) or t(locale.code, "none")
        p = prompts.render(
            "evaluate", locale.code, **self._base_vars(env, locale),
            source_info=self._source_info(env), triage=triage.summary,
            urls=", ".join(env.urls[:10]) or "none", text=env.text[:8000] or t(locale.code, "empty_text"),
            image_notes=notes,
        )
        for _ in range(max(1, cfg.pipeline.max_rewrite_attempts)):
            async with self.app.limits.text:
                result = await self.app.hub.cpa.chat_json(
                    cfg.models.text_model, p.messages(), Evaluation,
                    temperature=cfg.models.text_temperature, json_mode=cfg.models.json_mode)
            if await is_localized([result.audience, result.angle, result.reason,
                                   *result.key_facts, *result.background_points, *result.risks],
                                  locale, self.app):
                return result
        raise ValueError("editorial narrative has wrong language")

    async def _rewrite(self, task_id: str, env: InputEnvelope, evaluation: Evaluation,
                       analyses: dict[int, ImageAnalysis], rules: XRules,
                       hooks: list[dict[str, Any]], locale: Locale, cfg: AppSettings
                       ) -> tuple[RewriteResult, GuardReport]:
        # Source-derived text counts as verified; everything the LLM produced does not.
        verified = [a.extracted_text for a in analyses.values() if a.extracted_text]
        unverified = evaluation.key_facts + evaluation.background_points
        hooks_text = "\n".join(f"- {h['pattern']} {t(locale.code, 'hook_example')} \"{h['example']}\""
                               for h in hooks) or "- (none)"

        def bullet(items: list[str]) -> str:
            return "; ".join(items) or "(none)"

        feedback = ""
        best: tuple[RewriteResult, GuardReport] | None = None
        for attempt in range(1, max(1, cfg.pipeline.max_rewrite_attempts) + 1):
            p = prompts.render(
                "rewrite", locale.code, **self._base_vars(env, locale),
                max_chars=rules.max_chars, max_hashtags=rules.max_hashtags,
                max_emojis=rules.max_emojis, style=rules.style,
                banned_phrases=", ".join(f'"{b}"' for b in rules.banned_phrases) or "(none)",
                hooks=hooks_text, angle=evaluation.angle, audience=evaluation.audience,
                key_facts=bullet(evaluation.key_facts),
                background_points=bullet(evaluation.background_points),
                risks=bullet(evaluation.risks), text=env.text[:6000] or t(locale.code, "empty_text"),
                feedback=feedback,
            )
            async with self.app.limits.text:
                result = await self.app.hub.cpa.chat_json(
                    cfg.models.text_model, p.messages(), RewriteResult,
                    temperature=cfg.models.text_temperature, json_mode=cfg.models.json_mode,
                )
            report = check_rewrite(result, source_text=env.text, rules=rules,
                                   verified_facts=verified, unverified_facts=unverified, locale=locale.code)
            language_ok = await is_localized(
                [result.post, result.hook, result.added_value, result.image_brief,
                 *(claim.text for claim in result.claims)], locale, self.app)
            if not language_ok:
                # Never retain an invalid-language candidate as the best publishable draft.
                feedback = t(locale.code, "guard_language")
                await self._event(task_id, "rewrite", "wrong-language candidate rejected",
                                  level="warning")
                continue
            await self._event(
                task_id, "rewrite",
                f"attempt {attempt}: {'ok' if report.ok else '; '.join(report.problems)}",
                level="info" if report.ok else "warning",
                data={"post": result.post, "problems": report.problems},
            )
            if best is None or (len(report.problems), len(report.review)) < (
                    len(best[1].problems), len(best[1].review)):
                best = (result, report)
            if report.ok:
                break
            feedback = t(locale.code, "retry_feedback",
                         problems="\n- ".join(report.problems), post=result.post)
        if best is None:
            raise ValueError("no localized draft after rewrite retries")
        return best

    async def _fact_packet(self, task_id: str, env: InputEnvelope,
                           analyses: dict[int, ImageAnalysis]) -> tuple[VerifiedFacts, list[str]]:
        try:
            return await build_verified_facts(env, analyses, self.app), []
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("fact packet verification failed")
            await self._event(task_id, "facts", "verified fact packet unavailable", level="warning")
            return VerifiedFacts(), [t(env.locale, "image_facts_unavailable")]

    async def _ensure_media_rows(self, task_id: str, env: InputEnvelope,
                                 images: list[LoadedImage], outcomes: list[ImageOutcome],
                                 analyses: dict[int, ImageAnalysis]) -> None:
        loaded = {image.idx: image for image in images}
        for idx, source in enumerate(env.media):
            blob = loaded[idx].blob if idx in loaded else None
            await self.app.repo.upsert_media(task_id, idx, message_id=source.message_id,
                kind=source.kind.value, mime=blob.mime if blob else source.mime,
                size_bytes=blob.size if blob else source.size,
                width=blob.width if blob else None, height=blob.height if blob else None,
                source_sha256=blob.sha256 if blob else None)
            if idx in analyses:
                await self.app.repo.update_media_analysis(task_id, idx, analyses[idx])
        for outcome in outcomes:
            if outcome.idx >= len(env.media):
                await self.app.repo.upsert_media(task_id, outcome.idx,
                    message_id=env.message_ids[0], kind=MediaKind.GENERATED.value,
                    mime="image/png", size_bytes=None)

    async def _process_images(self, task_id: str, env: InputEnvelope, images: list[LoadedImage],
                              analyses: dict[int, ImageAnalysis], rewrite: RewriteResult,
                              evaluation: Evaluation, locale: Locale, cfg: AppSettings, *,
                              verified_facts: VerifiedFacts | None = None, max_images: int = 4
                              ) -> list[ImageOutcome]:
        """Registry-driven strategies; generators receive confirmed packets, never raw intake."""
        locale = self.app.i18n.get(env.image_options.target_locale or locale.code)
        ui_locale = env.locale
        packet = verified_facts or VerifiedFacts()
        if packet.facts and not packet_is_traceable(packet, env.text, analyses):
            packet = VerifiedFacts()
        action, options = env.image_action, env.image_options
        selected = ACTIONS.get(action)
        m, hub, limits = cfg.models, self.app.hub, self.app.limits
        rules = prompts.render_json("image_actions", locale.code)
        option_rules = "\n".join(rules["options"][flag.value] for flag in sorted(options.flags))

        def render_image(chosen: ImageAction, reference: LoadedImage | None) -> str:
            return prompts.render("image_execute", locale.code, action=chosen.value,
                action_rules=rules["actions"][chosen.value], target_locale=locale.code,
                language_name=locale.language_name, market=env.market, facts=packet.text,
                angle=rewrite.hook, brief=rewrite.image_brief,
                layout=(analyses[reference.idx].layout_description if reference and
                        {ImageOption.SIMILAR_LAYOUT, ImageOption.MINIMAL_CHANGES} & options.flags else ""),
                options=option_rules, density=options.information_density.value,
                density_rules=rules["densities"][options.information_density.value],
                size=m.image_size).user

        async def execute(idx: int, chosen: ImageAction, execution: str, mode: QCMode | None,
                          reason: str, reference: LoadedImage | None) -> ImageOutcome:
            if execution == "review":
                return ImageOutcome(idx, ImageDecision.REVIEW, reason)
            decision = ImageDecision._value2member_map_.get(chosen.value, chosen)
            if execution == "omit":
                return ImageOutcome(idx, decision, reason)
            if execution == "keep":
                output = await asyncio.to_thread(self._optimize, reference.blob.data,
                                                 graphic_hint(analyses[idx].image_type), cfg)
                return ImageOutcome(idx, decision, reason, output)
            if not packet.facts:
                return ImageOutcome(idx, ImageDecision.REVIEW, t(ui_locale, "image_facts_unavailable"))
            try:
                prompt = render_image(chosen, reference)
                async with limits.image:
                    if execution == "edit":
                        raw = await hub.cpa.images_edit(m.image_model, prompt, reference.blob.data,
                            reference.blob.mime, size=m.image_size, mode=m.image_edit_mode)
                    else:
                        raw = await hub.cpa.images_generate(m.image_model, prompt, size=m.image_size)
                graphic = True if execution == "create" else graphic_hint(analyses[idx].image_type)
                output = await asyncio.to_thread(self._optimize, raw, graphic, cfg)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("image execution failed")
                return ImageOutcome(idx, ImageDecision.REVIEW, t(ui_locale, "image_failed"))
            passed, qc_reason = await self._qc(env, locale, cfg, mode, output, reference,
                                               facts=packet.text, post=rewrite.post,
                source_text=(analyses[reference.idx].extracted_text if reference else ""))
            if not passed:
                return ImageOutcome(idx, ImageDecision.REVIEW,
                    t(ui_locale, "qc_fail", mode=label(ui_locale, mode), reason=qc_reason),
                    review_blob=output, ai_generated=True)
            return ImageOutcome(idx, decision, reason + " " + t(ui_locale, "qc_pass"), output,
                                ai_generated=True)

        if selected and selected.execution == "omit":
            return [ImageOutcome(idx, ImageDecision._value2member_map_.get(action.value, action),
                    t(ui_locale, "image_action_reason", action=t(ui_locale, "image_action_" + action.value)))
                    for idx in range(len(env.media))]
        if selected and selected.text_capable:
            # One original publishing visual for the canonical task, including text-only input.
            omitted = [ImageOutcome(idx, ImageDecision.OMIT, t(ui_locale, "image_action_omit"))
                       for idx in range(len(env.media))]
            if max_images <= 0:
                return omitted
            return omitted + [await execute(len(env.media), action, "create", "regenerate",
                t(ui_locale, "image_action_reason", action=t(ui_locale, "image_action_" + action.value)), None)]

        if action is not None and not env.media:
            return [ImageOutcome(0, ImageDecision.REVIEW, t(ui_locale, "image_source_required"))]

        async def one(image: LoadedImage) -> ImageOutcome:
            owned = media_is_owned(image.source, env, owned_ids=set(cfg.pipeline.owned_source_ids),
                                   direct_uploads_owned=cfg.pipeline.direct_uploads_owned)
            resolved = plan(analyses[image.idx], requested=action, options=options, owned=owned,
                            target_language=locale.language_tag, locale=ui_locale)
            if resolved.execution == "review":
                return ImageOutcome(image.idx, ImageDecision.REVIEW, resolved.reason)
            return await execute(image.idx, resolved.action, resolved.execution, resolved.qc_mode,
                                 resolved.reason, image)

        results = await asyncio.gather(*(one(img) for img in images if img.idx in analyses),
                                        return_exceptions=True)
        final = []
        for image, result in zip([img for img in images if img.idx in analyses], results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                final.append(ImageOutcome(image.idx, ImageDecision.REVIEW, t(ui_locale, "image_failed")))
            else:
                final.append(result)
        present = {result.idx for result in final}
        missing = [idx for idx in range(len(env.media)) if idx not in present]
        records = await self.app.repo.list_media(task_id) if missing else []
        failure_reasons = {row["idx"]: row.get("decision_reason") for row in records
                           if row.get("decision") == ImageDecision.REVIEW.value}
        for idx in range(len(env.media)):
            if idx not in present:
                final.append(ImageOutcome(idx, ImageDecision.REVIEW,
                    (failure_reasons.get(idx) or t(ui_locale, "vision_failed")) if idx < max_images else
                    t(ui_locale, "media_limit", count=max_images)))
        await self._event(task_id, "images", ", ".join(f"#{o.idx}:{o.decision.value}" for o in final))
        return final

    async def _process_images_only(self, task_id: str, task: dict[str, Any], env: InputEnvelope,
                                   locale: Locale, cfg: AppSettings) -> None:
        """Durable worker job: no evaluation/rewrite, and existing text remains byte-identical."""
        meta = dict(task.get("draft_meta") or {})
        if not task.get("draft_text"):
            raise ValueError("image-only task has no draft")
        canonical = meta.get("canonical_text")
        if canonical is None:  # Backfill facts for drafts created before this feature.
            env, _ = await clean_bundle(env, self.app)
        else:
            env = env.model_copy(update={"text": canonical})
        stored = meta.get("source_analyses") or {
            str(row["idx"]): row["analysis"] for row in await self.app.repo.list_media(task_id)
            if row.get("analysis")}
        sources = {int(idx): ImageAnalysis.model_validate(value) for idx, value in stored.items()}
        spec = ACTIONS.get(env.image_action)
        need_source = spec is None or (spec.execution != "omit" and not spec.text_capable)
        images, warnings = (await self._load_media(task_id, env, cfg, use=True)
                            if need_source else ([], []))
        try:
            analyses = await self._analyze(task_id, env, images, locale, cfg) if images else {}
            packet = VerifiedFacts.model_validate(meta.get("verified_facts") or {})
            fact_warnings = []
            if (spec is None or spec.execution in ("edit", "create")) and not packet_is_traceable(packet, env.text, sources):
                # Fresh facts may use persisted source OCR, never old editorial background.
                packet, fact_warnings = await self._fact_packet(task_id, env, {**sources, **analyses})
            rules = XRules.from_db({**prompts.render_json("knowledge", locale.code)["rules"],
                                   **await self.app.repo.get_rules(env.locale, env.market)})
            rewrite = RewriteResult(post=task["draft_text"], hook=meta.get("hook") or "",
                                    image_brief="")
            outcomes = await self._process_images(task_id, env, images[:rules.max_images], {**sources, **analyses},
                rewrite, Evaluation(suitable=True, value_score=0), locale, cfg,
                verified_facts=packet, max_images=rules.max_images)
            # Only replace assets after execution/QC; reference counting and R2 policy stay intact.
            await self.app.storage.release_task(task_id)
            await self.app.repo.delete_media_rows(task_id)
            await self._ensure_media_rows(task_id, env, images, outcomes, analyses)
            results = await self._persist(task_id, images, outcomes, analyses, cfg,
                                         locale=env.locale)
            text_review = list(meta.get("text_review", meta.get("review") or []))
            media_review = [result for result in results if result.decision == ImageDecision.REVIEW]
            review = text_review + ([t(env.locale, "images_review", count=len(media_review))]
                                    if media_review else [])
            meta.update(verified_facts=packet.model_dump(mode="json"), canonical_text=env.text,
                source_analyses={str(idx): a.model_dump(mode="json") for idx, a in {**sources, **analyses}.items()},
                image_action=env.image_action.value if env.image_action else None,
                image_options=env.image_options.model_dump(mode="json"),
                image_locale=env.image_options.target_locale or env.locale, review=review,
                text_review=text_review, warnings=list(meta.get("text_warnings", meta.get("warnings") or []))
                + warnings + fact_warnings, media=[result.model_dump(mode="json") for result in results])
            status = TaskStatus.NEEDS_REVIEW if review or meta.get("problems") else TaskStatus.DRAFT_READY
            await self.app.repo.save_draft(task_id, status, task["draft_text"], meta)
            await self._event(task_id, "images", "image-only job finished; draft unchanged")
            await self._notify_draft(task_id)
        finally:
            images.clear()

    async def _qc(self, env: InputEnvelope, locale: Locale, cfg: AppSettings, mode: QCMode,
                  candidate: ImageBlob, reference: LoadedImage | None, *, facts: str, post: str,
                  source_text: str = ""
                  ) -> tuple[bool, str]:
        """Visual verification of one Image2 output. Errors count as a failed check."""
        allowed = [facts]  # generated draft is never verification evidence
        if mode in ("enhance", "localize"):
            allowed.append(source_text)  # unchanged facts on authorized source edits
        try:
            messages = build_messages(
                mode, locale=locale.code, market=env.market, language_name=locale.language_name,
                facts=facts, reference_text=source_text, density=env.image_options.information_density.value,
                candidate_url=image_data_url(candidate.data, candidate.mime),
                reference_url=(image_data_url(reference.blob.data, reference.blob.mime)
                               if reference is not None else None),
            )
            async with self.app.limits.vision:
                qc = await self.app.hub.cpa.chat_json(cfg.models.vision_model, messages, ImageQC,
                                                      json_mode=cfg.models.json_mode)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("image QC unavailable", extra=log_ctx(error=mask(str(exc))))
            return False, t(env.locale, "qc_unavailable")
        if qc.issues and locale.code != env.locale:
            return False, t(env.locale, "qc_rejected")
        if qc.issues:
            try:
                if not await is_localized(qc.issues, locale, self.app):
                    return False, t(env.locale, "qc_rejected")
            except asyncio.CancelledError:
                raise
            except Exception:
                return False, t(env.locale, "qc_unavailable")
        return qc_verdict(qc, allowed_texts=allowed, locale=env.locale, target_locale=locale.code)

    async def _persist(self, task_id: str, images: list[LoadedImage],
                       outcomes: list[ImageOutcome], analyses: dict[int, ImageAnalysis],
                       cfg: AppSettings, *, locale: str = "en-US") -> list[MediaResult]:
        """Upload final assets (and optional compressed review copies) to R2, deduplicated."""
        by_idx = {img.idx: img for img in images}
        results: list[MediaResult] = []
        stored = 0
        for o in outcomes:
            decision, reason = o.decision, o.reason
            key: str | None = None
            kind: str | None = None
            try:
                if o.output is not None:
                    kind = "final"
                    key = await self.app.storage.persist(task_id, o.idx, o.output, kind)
                elif decision == ImageDecision.REVIEW and cfg.storage.persist_review_media:
                    copy = o.review_blob
                    if copy is None and o.idx in by_idx:
                        hint = (graphic_hint(analyses[o.idx].image_type)
                                if o.idx in analyses else None)
                        copy = await asyncio.to_thread(self._optimize, by_idx[o.idx].blob.data,
                                                       hint, cfg)
                    if copy is not None:
                        kind = "review"
                        key = await self.app.storage.persist(task_id, o.idx, copy, kind)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # R2/storage failures never block the text draft
                label = ("R2 budget reached" if isinstance(exc, StorageBudgetExceeded)
                         else f"R2 upload failed ({type(exc).__name__})")
                note = mask(f"{label}: {exc}")[:300]
                decision, reason, key, kind = (
                    ImageDecision.REVIEW, f"{reason} " + t(locale,
                        "storage_budget" if isinstance(exc, StorageBudgetExceeded)
                        else "storage_failed"), None, None)
                await self._event(task_id, "persist", f"image {o.idx}: {note}", level="warning")
            stored += key is not None
            await self.app.repo.update_media_decision(task_id, o.idx, decision.value, reason,
                                                      ai_generated=o.ai_generated)
            results.append(MediaResult(idx=o.idx, decision=decision, reason=reason,
                                       asset_key=key, asset_kind=kind,  # type: ignore[arg-type]
                                       ai_generated=o.ai_generated))
        await self._event(task_id, "persist", f"{stored} asset(s) persisted to R2")
        return results
