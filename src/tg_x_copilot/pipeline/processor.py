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
from ..i18n import Locale
from ..logging_setup import ctx as log_ctx
from ..logging_setup import mask
from ..models import (
    Evaluation, ImageAnalysis, ImageDecision, ImageQC, InputEnvelope, MediaKind,
    MediaResult, RewriteResult, SourceMedia, TaskStatus, TriageResult,
)
from ..services.storage import StorageBudgetExceeded
from .guards import GuardReport, XRules, check_rewrite, x_length
from .image_policy import decide
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
    decision: ImageDecision
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
        if not env.forwards:
            return "sent directly by the operator (not forwarded)"
        parts = []
        for f in env.forwards[:5]:
            bits = [str(f.chat_id) if f.chat_id else None, f.sender_name,
                    f.date.date().isoformat() if f.date else None]
            parts.append(" / ".join(b for b in bits if b) or "unknown")
        return "forwarded from: " + "; ".join(parts)

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
        if task["attempts"]:  # leftovers from an interrupted earlier run
            try:
                await self.app.storage.release_task(task_id)
            except Exception:
                log.exception("releasing leftover assets failed (non-fatal)")
        await self._event(task_id, "start", f"attempt {task['attempts'] + 1}",
                          data={"messages": len(env.message_ids), "media": len(env.media)})

        # 1. Triage: text only, before any download or LLM spend. Jev is optional.
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
                                      started)
        finally:
            images.clear()  # drop references to downloaded bytes promptly

    async def _run_editorial(self, task_id: str, env: InputEnvelope, locale: Locale,
                             cfg: AppSettings, triage: TriageResult, images: list[LoadedImage],
                             dup_warnings: list[str], started: float) -> None:
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
            await self._skip(task_id, env, f"editor: {evaluation.reason}")
            return

        await repo.set_stage(task_id, "rewrite")
        rules = XRules.from_db(await repo.get_rules(env.locale, env.market))
        hooks = await repo.get_hooks(env.locale, env.market)
        rewrite, report = await self._rewrite(task_id, env, evaluation, analyses, rules, hooks,
                                              locale, cfg)

        await repo.set_stage(task_id, "images")
        usable = [img for img in images if img.idx in analyses]
        for img in usable[rules.max_images:]:
            await repo.update_media_decision(task_id, img.idx, ImageDecision.REVIEW.value,
                                              f"Over the {rules.max_images}-image limit.")
        outcomes = await self._process_images(
            task_id, env, usable[: rules.max_images], analyses, rewrite, evaluation, locale, cfg
        )

        # 3. Only now, with a draft in hand, persist what the operator needs.
        await repo.set_stage(task_id, "persist")
        media_results = await self._persist(task_id, images, outcomes, analyses, cfg)

        problems = list(report.problems)  # blocking: cannot be approved
        review = list(report.review)  # must be checked by a human, then may be approved
        if triage.route == "review":
            review.append("Jev flagged for human review: " + " ".join(triage.reasons))
        media_review = [r for r in media_results if r.decision == ImageDecision.REVIEW]
        if media_review:
            review.append(f"{len(media_review)} image(s) need review (see image decisions).")
        status = (TaskStatus.DRAFT_READY if not problems and not review
                  else TaskStatus.NEEDS_REVIEW)
        meta = {
            "hook": rewrite.hook,
            "claims": [c.model_dump() for c in rewrite.claims],
            "added_value": rewrite.added_value,
            "problems": problems,
            "review": review,
            "warnings": report.warnings + dup_warnings,
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
            await self.app.storage.release_task(task_id)  # nothing persisted for failed tasks
            await self.app.repo.set_status(task_id, TaskStatus.FAILED, stage="failed", error=error)
            await self._event(task_id, "failed", error, level="error")
            task = await self.app.repo.get_task(task_id)
            if task and self.app.telegram:
                await self.app.telegram.notify(
                    task["tg_chat_id"],
                    self.app.i18n.t(task["locale"], "failed", task_id=task_id,
                                    error=html.escape(error[:300], quote=False)),
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
            return without_jev("Jev disabled; main LLM decides.", has_media=has_media)
        if not self.app.hub.jev.configured:
            return without_jev("Jev not configured (JEV__API_KEY unset); main LLM decides.",
                               has_media=has_media)
        if not has_usable_text(env):
            return without_jev("No usable text for Jev (text-only model); vision + LLM decide.",
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
            return without_jev(reason + "; main LLM decides.", has_media=has_media)
        return decide_route(resp, cfg, has_text=True, has_media=has_media)

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
        download_error = "Could not download from Telegram (deleted or unavailable)."
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

        loaded: list[LoadedImage] = []
        warnings: list[str] = []
        for idx, sm in enumerate(env.media):
            if not use:
                await review(idx, sm, "Not analyzed: Jev judged the media not useful.")
                continue
            if sm.kind not in _IMAGE_KINDS:
                await review(idx, sm, f"Unsupported media type in MVP ({sm.kind.value}); "
                                      "handle manually.")
                continue
            if (sm.size or 0) > max_bytes:
                await review(idx, sm, f"File too large ({sm.size} bytes).")
                continue
            data = downloaded.pop(sm.message_id, None)
            if data is None:
                await review(idx, sm, download_error)
                continue
            blob = await asyncio.to_thread(inspect_image, data)
            if blob is None:
                await review(idx, sm, "File is not a readable image.")
                continue
            await repo.upsert_media(task_id, idx, message_id=sm.message_id, kind=sm.kind.value,
                                    mime=blob.mime, size_bytes=blob.size, width=blob.width,
                                    height=blob.height, source_sha256=blob.sha256)
            for prev in await repo.previous_uses(blob.sha256, task_id):
                warnings.append(f"Image #{idx} was already forwarded in task "
                                f"{prev['task_id'][:8]} ({prev['status']}).")
                break
            loaded.append(LoadedImage(idx, blob, sm))

        await self._event(task_id, "media",
                          f"{len(loaded)}/{len(env.media)} image(s) loaded in memory (not stored)")
        return loaded, warnings

    async def _analyze(self, task_id: str, env: InputEnvelope, images: list[LoadedImage],
                       locale: Locale, cfg: AppSettings) -> dict[int, ImageAnalysis]:
        if not images:
            return {}
        owned_hint = ", ".join(str(i) for i in cfg.pipeline.owned_source_ids) or "none configured"

        async def one(img: LoadedImage) -> tuple[int, ImageAnalysis]:
            p = prompts.render("vision_analyze", locale.code, **self._base_vars(env, locale),
                               owned_hint=owned_hint, text=env.text[:3000] or "(no text)")
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
                    task_id, img.idx, ImageDecision.REVIEW.value, reason)
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
            + (f"; text ({a.text_language}): {a.extracted_text[:300]}" if a.contains_text else "")
            for i, a in sorted(analyses.items())
        ) or "(none)"
        p = prompts.render(
            "evaluate", locale.code, **self._base_vars(env, locale),
            source_info=self._source_info(env), triage=triage.summary,
            urls=", ".join(env.urls[:10]) or "none", text=env.text[:8000] or "(no text)",
            image_notes=notes,
        )
        async with self.app.limits.text:
            return await self.app.hub.cpa.chat_json(
                cfg.models.text_model, p.messages(), Evaluation,
                temperature=cfg.models.text_temperature, json_mode=cfg.models.json_mode,
            )

    async def _rewrite(self, task_id: str, env: InputEnvelope, evaluation: Evaluation,
                       analyses: dict[int, ImageAnalysis], rules: XRules,
                       hooks: list[dict[str, Any]], locale: Locale, cfg: AppSettings
                       ) -> tuple[RewriteResult, GuardReport]:
        # Source-derived text counts as verified; everything the LLM produced does not.
        verified = [a.extracted_text for a in analyses.values() if a.extracted_text]
        unverified = evaluation.key_facts + evaluation.background_points
        hooks_text = "\n".join(f"- {h['name']}: {h['pattern']} e.g. \"{h['example']}\""
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
                risks=bullet(evaluation.risks), text=env.text[:6000] or "(no text)",
                feedback=feedback,
            )
            async with self.app.limits.text:
                result = await self.app.hub.cpa.chat_json(
                    cfg.models.text_model, p.messages(), RewriteResult,
                    temperature=cfg.models.text_temperature, json_mode=cfg.models.json_mode,
                )
            report = check_rewrite(result, source_text=env.text, rules=rules,
                                   verified_facts=verified, unverified_facts=unverified)
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
            feedback = (
                "Your previous draft was rejected. Fix ALL of these problems:\n- "
                + "\n- ".join(report.problems)
                + f"\n\nPrevious draft:\n{result.post}"
            )
        assert best is not None
        return best

    async def _process_images(self, task_id: str, env: InputEnvelope, images: list[LoadedImage],
                              analyses: dict[int, ImageAnalysis], rewrite: RewriteResult,
                              evaluation: Evaluation, locale: Locale, cfg: AppSettings
                              ) -> list[ImageOutcome]:
        """Decide and produce image outputs in memory. Nothing is uploaded here."""
        owned_ids = set(cfg.pipeline.owned_source_ids)
        if env.forwards:
            owned = bool(env.source_chat_ids) and env.source_chat_ids <= owned_ids
        else:
            owned = cfg.pipeline.direct_uploads_owned
        m = cfg.models
        hub, limits = self.app.hub, self.app.limits

        async def edit(prompt: str, img: LoadedImage) -> bytes:
            async with limits.image:
                return await hub.cpa.images_edit(m.image_model, prompt, img.blob.data,
                                                 img.blob.mime, size=m.image_size,
                                                 mode=m.image_edit_mode)

        async def one(img: LoadedImage) -> ImageOutcome:
            analysis = analyses[img.idx]
            hint = graphic_hint(analysis.image_type)
            decision, reason = decide(analysis, owned_source=owned,
                                      target_language=locale.language_tag)
            if decision == ImageDecision.REVIEW:
                return ImageOutcome(img.idx, decision, reason)
            try:
                if decision == ImageDecision.KEEP:  # no Image2 involved -> no QC needed
                    output = await asyncio.to_thread(self._optimize, img.blob.data, hint, cfg)
                    return ImageOutcome(img.idx, decision, reason, output)

                mode: QCMode
                reference: LoadedImage | None = img
                if decision == ImageDecision.ENHANCE:
                    mode, facts = "enhance", analysis.extracted_text
                    raw = await edit(prompts.render("image_enhance", locale.code,
                                                    size=m.image_size).user, img)
                elif owned:  # REGENERATE of our own visual = localize it, using it as reference
                    mode, facts = "localize", analysis.extracted_text
                    raw = await edit(prompts.render("image_localize", locale.code,
                                                    **self._base_vars(env, locale),
                                                    size=m.image_size).user, img)
                else:  # original visual; the third-party image is NOT sent as a reference
                    mode, reference = "regenerate", None
                    facts = (analysis.extracted_text
                             if analysis.image_type in ("chart", "infographic")
                             else "; ".join(evaluation.key_facts[:4]))
                    prompt = prompts.render(
                        "image_regenerate", locale.code, **self._base_vars(env, locale),
                        post=rewrite.post, brief=rewrite.image_brief or analysis.description,
                        facts=facts or "(none)",
                    ).user
                    async with limits.image:
                        raw = await hub.cpa.images_generate(m.image_model, prompt,
                                                            size=m.image_size)
                output = await asyncio.to_thread(self._optimize, raw, hint, cfg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return ImageOutcome(img.idx, ImageDecision.REVIEW,
                                    mask(f"Image step failed ({type(exc).__name__}): {exc}")[:500])

            # Every Image2 output must pass visual QC before it can be a final asset.
            passed, qc_reason = await self._qc(env, locale, cfg, mode, output, reference,
                                               facts=facts, post=rewrite.post)
            if not passed:
                return ImageOutcome(img.idx, ImageDecision.REVIEW,
                                    f"Image QC failed after {mode}: {qc_reason}",
                                    review_blob=output, ai_generated=True)
            return ImageOutcome(img.idx, decision, f"{reason} QC passed.", output,
                                ai_generated=True)

        results = await asyncio.gather(*(one(i) for i in images), return_exceptions=True)
        final: list[ImageOutcome] = []
        for img, res in zip(images, results):
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, BaseException):
                final.append(ImageOutcome(img.idx, ImageDecision.REVIEW,
                                          mask(f"Image pipeline error: {res}")[:500]))
            else:
                final.append(res)
        await self._event(task_id, "images",
                          ", ".join(f"#{o.idx}:{o.decision.value}" for o in final) or "no images")
        return final

    async def _qc(self, env: InputEnvelope, locale: Locale, cfg: AppSettings, mode: QCMode,
                  candidate: ImageBlob, reference: LoadedImage | None, *, facts: str, post: str
                  ) -> tuple[bool, str]:
        """Visual verification of one Image2 output. Errors count as a failed check."""
        allowed = [facts, post] if mode == "regenerate" else [facts]
        if reference is not None:
            allowed.append(env.text)
        try:
            messages = build_messages(
                mode, locale=locale.code, market=env.market, language_name=locale.language_name,
                facts=facts, reference_text=facts if reference is not None else "",
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
            return False, mask(f"QC unavailable ({type(exc).__name__}: {exc})")[:300]
        return qc_verdict(qc, allowed_texts=allowed)

    async def _persist(self, task_id: str, images: list[LoadedImage],
                       outcomes: list[ImageOutcome], analyses: dict[int, ImageAnalysis],
                       cfg: AppSettings) -> list[MediaResult]:
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
                decision, reason, key, kind = ImageDecision.REVIEW, f"{reason} [{note}]", None, None
                await self._event(task_id, "persist", f"image {o.idx}: {note}", level="warning")
            stored += key is not None
            await self.app.repo.update_media_decision(task_id, o.idx, decision.value, reason,
                                                      ai_generated=o.ai_generated)
            results.append(MediaResult(idx=o.idx, decision=decision, reason=reason,
                                       asset_key=key, asset_kind=kind,  # type: ignore[arg-type]
                                       ai_generated=o.ai_generated))
        await self._event(task_id, "persist", f"{stored} asset(s) persisted to R2")
        return results
