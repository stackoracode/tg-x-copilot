"""Operational actions shared by the Telegram bot and the admin UI:
model refresh, connection tests, and task approve/reject/regenerate."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..logging_setup import mask
from ..i18n import label
from ..models import TaskStatus, ImageEditAuthorization
from ..pipeline.image_policy import needs_edit_confirmation
from ..image_settings import ImagePreferences

if TYPE_CHECKING:
    from ..app_context import AppContext

log = logging.getLogger(__name__)


def guess_capabilities(model_id: str) -> list[str]:
    mid = model_id.lower()
    caps: list[str] = []
    if any(k in mid for k in ("image", "dall-e", "imagen", "flux", "banana")):
        caps.append("image")
    if any(k in mid for k in ("embed", "whisper", "tts", "moderation")):
        caps.append("other")
    if not caps:
        caps.append("chat")
        if any(k in mid for k in ("gpt-4o", "gpt-4.1", "gpt-5", "vision", "gemini", "claude",
                                  "qwen-vl", "grok", "-vl")):
            caps.append("vision")
    return caps


@dataclass
class CheckResult:
    name: str
    ok: bool
    latency_ms: int
    detail: str


class Ops:
    def __init__(self, app: "AppContext") -> None:
        self.app = app

    def t(self, key: str, **kwargs: Any) -> str:
        return self.app.i18n.t(self.app.config.current.default_locale, key, **kwargs)

    def model_summary(self, summary: dict[str, Any]) -> str:
        parts = []
        for provider, count in summary.items():
            if provider == "configured_but_missing":
                parts.append(self.t("models_missing", items=", ".join(count)))
            elif isinstance(count, int):
                parts.append(self.t("refresh_count", provider=provider, count=count))
            else:
                parts.append(self.t("refresh_failed", provider=provider))
        return "; ".join(parts)

    # ------------------------------------------------------------------ models

    async def _cpa_models(self) -> list[dict[str, Any]]:
        models = await self.app.hub.cpa.list_models()
        for m in models:
            m["capabilities"] = guess_capabilities(str(m.get("id", "")))
        return models

    async def _jev_models(self) -> list[dict[str, Any]]:
        # TypeSafe format: {name, description, release_date}
        return [{"id": m.get("name") or m.get("id"), "owned_by": "typesafe",
                 "capabilities": ["systemone"], **m} for m in await self.app.hub.jev.list_models()]

    async def refresh_models(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for name, loader in (("cpa", self._cpa_models), ("jev", self._jev_models)):
            try:
                summary[name] = await self.app.repo.upsert_models(name, await loader())
            except Exception as exc:
                summary[name] = f"error: {mask(str(exc))[:200]}"
                log.warning("model refresh failed for %s", name)
        cfg = self.app.config.current
        known = {(m["provider"], m["model_id"]) for m in await self.app.repo.list_models()}
        wanted = [("cpa", cfg.models.text_model), ("cpa", cfg.models.vision_model),
                  ("cpa", cfg.models.image_model), ("jev", cfg.jev.model)]
        missing = [f"{p}:{m}" for p, m in wanted if m and (p, m) not in known]
        if missing:
            summary["configured_but_missing"] = missing
        return summary

    # ------------------------------------------------------------------ health

    async def _check(self, name: str, fn: Callable[[], Awaitable[str]]) -> CheckResult:
        start = time.monotonic()
        try:
            detail = await asyncio.wait_for(fn(), timeout=20)
            ok = True
        except Exception as exc:
            ok, detail = False, mask(f"{type(exc).__name__}: {exc}")[:300]
        return CheckResult(name, ok, int((time.monotonic() - start) * 1000), detail)

    async def test_connections(self) -> list[CheckResult]:
        hub, cfg = self.app.hub, self.app.config.current

        async def db() -> str:
            await self.app.db.ping()
            return "SELECT 1 ok"

        async def r2() -> str:
            await hub.r2.head_bucket()
            return f"bucket {cfg.r2.bucket} reachable"

        async def models_of(loader: Callable[[], Awaitable[list[dict[str, Any]]]],
                            wanted: list[str]) -> str:
            ids = {m.get("id") for m in await loader()}
            missing = [w for w in wanted if w not in ids]
            if missing:
                raise RuntimeError(f"{len(ids)} models listed, but missing: {', '.join(missing)}")
            return f"{len(ids)} models; configured models present"

        async def r2_usage() -> str:
            used, budget = await self.app.repo.stored_bytes(), cfg.storage.budget_bytes
            if used > budget:
                raise RuntimeError(f"tracked usage {used / 1e9:.2f} GB exceeds budget")
            return f"tracked usage {used / 1e6:.1f} MB of {budget / 1e9:.1f} GB budget"

        checks = [
            self._check("mysql", db),
            self._check("r2", r2),
            self._check("r2_budget", r2_usage),
            self._check("cpa", lambda: models_of(
                self._cpa_models,
                [cfg.models.text_model, cfg.models.vision_model, cfg.models.image_model])),
            self._check("jev", lambda: models_of(self._jev_models, [cfg.jev.model])),
        ]
        results = list(await asyncio.gather(*checks))
        tg = self.app.telegram
        results.append(CheckResult("telegram", bool(tg and tg.connected), 0,
                                   "connected" if tg and tg.connected else "not connected"))
        return results

    # ------------------------------------------------------------------ task actions

    async def approve(self, task_id: str) -> tuple[bool, str]:
        task = await self.app.repo.get_task(task_id)
        if not task:
            return False, self.t("not_found")
        meta = task.get("draft_meta") or {}
        status = task["status"]
        if status == TaskStatus.NEEDS_REVIEW.value and meta.get("problems"):
            return False, self.t("blocking")
        if status not in (TaskStatus.DRAFT_READY.value, TaskStatus.NEEDS_REVIEW.value):
            return False, self.t("cannot_approve", status=label(self.app.config.current.default_locale, status))
        await self.app.repo.set_status(task_id, TaskStatus.APPROVED, stage="done")
        # Final assets were already delivered to Telegram with the draft. Unless long-term
        # retention is enabled, approving frees them from R2 as well as any review copies.
        retain = self.app.config.current.storage.retain_approved_assets
        kinds = ("review",) if retain else ("final", "review")
        try:
            freed = await self.app.storage.release_task(task_id, kinds=kinds)
        except Exception:
            log.exception("releasing assets on approve failed (non-fatal)")
            freed = 0
        await self.app.repo.add_event(
            task_id, "approve",
            f"approved by operator ({'reviewed' if status == 'needs_review' else 'ready'}); "
            f"{freed} R2 object(s) deleted; retain_approved_assets={retain}")
        return True, self.t("ops_approved")

    async def reject(self, task_id: str) -> tuple[bool, str]:
        task = await self.app.repo.get_task(task_id)
        if not task:
            return False, self.t("not_found")
        if task["status"] in (TaskStatus.APPROVED.value, TaskStatus.PROCESSING.value):
            return False, self.t("cannot_reject", status=label(self.app.config.current.default_locale, task["status"]))
        await self.app.repo.set_status(task_id, TaskStatus.REJECTED, stage="done")
        freed = await self.app.storage.release_task(task_id)
        await self.app.repo.add_event(task_id, "reject",
                                      f"rejected by operator; {freed} R2 object(s) deleted")
        return True, self.t("ops_rejected")

    async def regenerate(self, task_id: str) -> tuple[bool, str]:
        task = await self.app.repo.get_task(task_id)
        if not task:
            return False, self.t("not_found")
        allowed = {TaskStatus.DRAFT_READY, TaskStatus.NEEDS_REVIEW, TaskStatus.SKIPPED,
                   TaskStatus.FAILED, TaskStatus.REJECTED}
        if TaskStatus(task["status"]) not in allowed:
            return False, self.t("cannot_regenerate", status=label(self.app.config.current.default_locale, task["status"]))
        await self.app.storage.release_task(task_id)  # media is re-fetched from Telegram
        if hasattr(self.app, "image_preferences") and task.get("tg_user_id") is not None:
            prefs = await self.app.image_preferences.get(task["tg_user_id"])
            await self.app.repo.set_task_image_preferences(task_id, prefs)
        await self.app.repo.set_task_locale(task_id, self.app.config.current.default_locale,
                                            self.app.config.current.market)
        await self.app.repo.set_status(task_id, TaskStatus.RECEIVED, stage="queued")
        await self.app.repo.add_event(task_id, "regenerate", "re-queued by operator")
        if not await self.app.workers.enqueue(task_id, wait=False):
            return True, self.t("ops_busy")
        return True, self.t("ops_queued")

    async def authorize_image_edit(self, task_id: str, user_id: int, chat_id: int,
                                   idx: int) -> tuple[bool, str]:
        task = await self.app.repo.get_task(task_id)
        tr = lambda key: self.app.i18n.t(task.get("locale", self.app.config.current.default_locale)
                                       if task else self.app.config.current.default_locale, key)
        if not task or task.get("tg_user_id") != user_id or task.get("tg_chat_id") != chat_id:
            return False, tr("image_tools_owner")
        if task["status"] not in ("draft_ready", "needs_review") or not task.get("draft_text"):
            return False, tr("edit_auth_stale")
        media = next((m for m in await self.app.repo.list_media(task_id) if m["idx"] == idx), None)
        if not media or not needs_edit_confirmation(task, media):
            return False, tr("edit_auth_stale")
        authorization = ImageEditAuthorization(task_id=task_id, image_idx=idx, user_id=user_id,
                                              source_sha256=media["source_sha256"])
        if not await self.app.repo.authorize_image_edit(task_id, user_id, chat_id, authorization,
                                                       media["decision_reason"]):
            return False, tr("edit_auth_stale")
        await self.app.repo.add_event(task_id, "IMAGE_POLICY", "explicit image editing rights confirmed",
                                      data=authorization.model_dump(mode="json"))
        await self.app.workers.enqueue(task_id, wait=False)
        return True, tr("edit_auth_queued")

    async def rerun_images(self, task_id: str, user_id: int,
                           preferences: ImagePreferences) -> tuple[bool, str]:
        task = await self.app.repo.get_task(task_id)
        if not task:
            return False, self.t("not_found")
        if task.get("tg_user_id") != user_id:
            return False, self.t("image_tools_owner")
        if task["status"] not in (TaskStatus.DRAFT_READY.value, TaskStatus.NEEDS_REVIEW.value) or not task.get("draft_text"):
            return False, self.t("image_tools_not_ready")
        if not await self.app.repo.queue_image_rerun(task_id, user_id, preferences):
            return False, self.t("image_tools_not_ready")
        await self.app.repo.add_event(task_id, "images", "queued image-only job",
                                      data=preferences.model_dump(mode="json"))
        await self.app.workers.enqueue(task_id, wait=False)  # sweeper covers a full queue
        return True, self.t("image_tools_queued")
