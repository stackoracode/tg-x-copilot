"""Telethon bot: transport only. Receives forwards, shows drafts, exposes ops buttons.

Business logic lives in the pipeline/services; this module only translates between Telegram
and them. All outgoing text uses HTML parse mode with escaped dynamic content.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError

from ..i18n import label
from ..models import MediaFailureStage, TaskStatus
from ..pipeline.media import inspect_image
from ..pipeline.media_errors import media_error_reason
from ..pipeline.image_policy import needs_edit_confirmation
from .image_tools import ImageTools
from .bundles import BundleCollector
from ..image_settings import WorkflowMode, ImagePreferences
from ..logging_setup import ctx

if TYPE_CHECKING:
    from ..app_context import AppContext

log = logging.getLogger(__name__)
_MAX_MSG = 4000
_DECISION_ICON = {"generate": "🎨", "info_card": "📊", "clean_recreate": "🧹", "omit": "⏭", "text_only": "📄", "keep": "✅", "enhance": "✨", "regenerate": "🎨", "localize": "🌐", "recreate": "🎨", "review": "👀"}


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


class TelegramBot:
    def __init__(self, app: "AppContext") -> None:
        self.app = app
        cfg = app.config.base.telegram  # bootstrap-only settings
        Path(cfg.session_path).parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(cfg.session_path, cfg.api_id,
                                     cfg.api_hash.get_secret_value())
        self.client.parse_mode = "html"
        self._allowed = set(cfg.allowed_user_ids)
        self._token = cfg.bot_token.get_secret_value()
        self._collector = BundleCollector()

    @property
    def connected(self) -> bool:
        return self.client.is_connected()

    def t(self, key: str, **kw: Any) -> str:
        return self.app.i18n.t(self.app.config.current.default_locale, key, **kw)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if not self._allowed:
            log.warning("TELEGRAM__ALLOWED_USER_IDS is empty: the bot will refuse everyone")
        await self.client.start(bot_token=self._token)
        me = await self.client.get_me()
        log.info("telegram bot connected", extra=ctx(username=getattr(me, "username", None)))
        c = self.client
        c.add_event_handler(self._on_command, events.NewMessage(
            incoming=True, pattern=r"^/(start|help|menu|settings|recent|images)\b"))
        c.add_event_handler(self._on_message, events.NewMessage(
            incoming=True, func=lambda e: e.is_private and not (e.raw_text or "").startswith("/")
            and e.message.grouped_id is None))
        c.add_event_handler(self._on_album, events.Album(func=lambda e: e.is_private))
        c.add_event_handler(self._on_callback, events.CallbackQuery())

    async def flush_intake(self) -> None:
        collector = getattr(self, "_collector", None)
        if collector:
            await collector.close()

    async def stop(self) -> None:
        await self.flush_intake()
        if self.client.is_connected():
            await self.client.disconnect()
        log.info("telegram bot disconnected")

    # ------------------------------------------------------------------ helpers used by pipeline

    async def notify(self, chat_id: int, text: str, buttons: Any = None, *, reply_to: int | None = None) -> None:
        kwargs = {"buttons": buttons, "link_preview": False}
        if reply_to is not None:
            kwargs["reply_to"] = reply_to
        await self.client.send_message(chat_id, text[:_MAX_MSG], **kwargs)

    async def _format_progress(self, task: dict[str, Any], step: str) -> str:
        task_id = task["id"]
        locale = task.get("locale", self.app.config.current.default_locale)
        key = "task_progress_" + step
        header = self.app.i18n.t(locale, key, task_id=task_id[:8])
        lines = [header]

        jev = task.get("jev_result")
        if jev and isinstance(jev, dict) and jev.get("route"):
            route = label(locale, str(jev.get("route") or ""))
            val = jev.get("value")
            val_str = f"{val:.2f}" if isinstance(val, (int, float)) else str(val or "0.00")
            conf = jev.get("confidence") or "-"
            if locale == "zh-CN":
                lines.append(f"⚖️ Jev 初筛: {route}（分值: {val_str}，置信度: {conf}）")
            else:
                lines.append(f"⚖️ Jev triage: {route} (score: {val_str}, conf: {conf})")

        ev = task.get("evaluation")
        if ev and isinstance(ev, dict) and "suitable" in ev:
            val = ev.get("value_score")
            val_str = f"{val:.2f}" if isinstance(val, (int, float)) else str(val or "0.00")
            suitable_str = "适合" if ev.get("suitable") else "需审核" if locale == "zh-CN" else ("yes" if ev.get("suitable") else "no")
            if locale == "zh-CN":
                lines.append(f"📊 主模型评估: {suitable_str}（价值评分: {val_str}）")
            else:
                lines.append(f"📊 Evaluation: {suitable_str} (value: {val_str})")

        try:
            events = await self.app.repo.list_events(task_id)
            if events:
                step_names_zh = {
                    "start": "开始", "clean": "清理", "triage": "初筛", "media": "媒体",
                    "vision": "视觉", "evaluate": "评估", "rewrite": "重写",
                    "IMAGE2": "生图", "IMAGE_QC": "质检", "IMAGE_POLICY": "策略",
                    "images": "图流", "R2_UPLOAD": "上传", "persist": "保存", "done": "完成"
                }
                step_names_en = {
                    "start": "Start", "clean": "Clean", "triage": "Triage", "media": "Media",
                    "vision": "Vision", "evaluate": "Eval", "rewrite": "Draft",
                    "IMAGE2": "Image", "IMAGE_QC": "QC", "IMAGE_POLICY": "Policy",
                    "images": "Images", "R2_UPLOAD": "Upload", "persist": "Persist", "done": "Done"
                }
                step_map = step_names_zh if locale == "zh-CN" else step_names_en
                lines.append("")
                for e in events[-4:]:
                    created = e.get("created_at")
                    if hasattr(created, "strftime"):
                        t_str = created.strftime("%H:%M:%S")
                    elif created:
                        t_str = str(created)[11:19]
                    else:
                        t_str = "--:--:--"
                    s_code = e.get("step") or ""
                    s_label = step_map.get(s_code, s_code[:4])
                    lvl = e.get("level") or "info"
                    if locale == "zh-CN":
                        lvl_badge = "告警" if lvl == "warning" else ("错误" if lvl in ("error", "critical") else "信息")
                    else:
                        lvl_badge = "WARN" if lvl == "warning" else ("ERR" if lvl in ("error", "critical") else "INFO")
                    msg = (e.get("message") or "").replace("\n", " ").strip()
                    if len(msg) > 38:
                        msg = msg[:35] + "..."
                    lines.append(f"<code>{t_str}</code> [{s_label}] {lvl_badge}: {esc(msg)}")
        except Exception:
            pass
        return "\n".join(lines)

    async def update_task_status(self, task_id: str, step: str, *, text: str | None = None,
                                 buttons: Any = None, reply_to: int | None = None) -> None:
        try:
            task = await self.app.repo.get_task(task_id)
        except Exception:
            log.exception("loading task for progress update failed", extra=ctx(task_id=task_id))
            return
        if not task:
            return
        envelope = task.get("envelope") or {}
        status_id = envelope.get("status_message_id")
        if text is None:
            text = await self._format_progress(task, step)
        if not status_id:
            # Pre-existing tasks retain their review-card delivery behavior.
            if text and step == "final":
                ids = envelope.get("message_ids") or []
                await self.notify(task["tg_chat_id"], text, buttons,
                                  reply_to=min(ids) if ids else reply_to)
            return
        try:
            await self.client.edit_message(task["tg_chat_id"], status_id, text[:_MAX_MSG],
                                           buttons=buttons, link_preview=False)
        except MessageNotModifiedError:
            pass
        except Exception:
            # A status-card transport problem must never suppress the publishing bundle.
            log.exception("editing task status failed", extra=ctx(task_id=task_id))

    async def _receipt(self, event: Any, messages: list[Any]) -> int | None:
        try:
            sent = await self.client.send_message(
                event.chat_id, self.t("task_received"), link_preview=False,
                reply_to=min(m.id for m in messages),
            )
            return getattr(sent, "id", None)
        except Exception:
            log.exception("sending intake receipt failed")
            return None

    async def fetch_media(self, chat_id: int, message_ids: list[int]) -> dict[int, bytes]:
        out: dict[int, bytes] = {}
        messages = await self.client.get_messages(chat_id, ids=message_ids)
        for m in messages or []:
            if m is None or m.media is None:
                continue
            try:
                data = await self.client.download_media(m, file=bytes)
                if data:
                    out[m.id] = data
            except Exception:
                log.exception("media download failed", extra=ctx(message_id=m.id))
        return out

    async def send_draft(self, task_id: str, *, only_indices: set[int] | None = None) -> None:
        app = self.app
        task = await app.repo.get_task(task_id)
        if not task:
            return
        def tr(key: str, **kw: Any) -> str:
            return app.i18n.t(task.get("locale", app.config.current.default_locale), key, **kw)
        locale = task.get("locale", app.config.current.default_locale)
        chat_id = task["tg_chat_id"]
        source_ids = (task.get("envelope") or {}).get("message_ids") or []
        source_reply = min(source_ids) if source_ids else None
        meta = task.get("draft_meta") or {}
        media = await app.repo.list_media(task_id)

        # Delivery is a separate, recorded step; processing/QC success alone is not delivery.
        files: list[tuple[int, io.BytesIO]] = []
        delivery: dict[str, dict[str, Any]] = (dict(meta.get("delivery") or {}) if only_indices is not None else {})
        failures: dict[int, tuple[MediaFailureStage, str]] = {
            int(key): (MediaFailureStage(receipt["failure_stage"]), receipt["reason"])
            for key, receipt in delivery.items() if receipt.get("failure_stage")
            and (only_indices is None or int(key) not in only_indices)
        }
        pipeline_meta = {entry["idx"]: entry for entry in meta.get("media") or []}

        def fail(idx: int, stage: MediaFailureStage, reason: str) -> None:
            failures[idx] = (stage, reason)
            delivery[str(idx)] = {"sent": False, "failure_stage": stage.value, "reason": reason}

        for m in media:
            idx = m["idx"]
            if only_indices is not None and idx not in only_indices:
                continue
            if m.get("decision") in ("omit", "text_only"):
                delivery[str(idx)] = {"sent": False, "omitted": True}
                continue
            if (m.get("decision") == "review" or pipeline_meta.get(idx, {}).get("failure_stage")
                    or not m.get("asset_key") or m.get("asset_kind") != "final"):
                reason = m.get("decision_reason") or tr("media_stage_missing_asset")
                stored_stage = pipeline_meta.get(idx, {}).get("failure_stage")
                if not stored_stage:
                    stored_stage = next((stage.value for stage in MediaFailureStage
                                         if reason.startswith(f"[{stage.value}]")), None)
                if not stored_stage:  # Old tasks did not record typed stage diagnostics.
                    stored_stage = ("QC" if m.get("ai_generated") and m.get("decision") == "review"
                                    else "R2_UPLOAD" if m.get("decision") != "review" else "IMAGE_POLICY")
                fail(idx, MediaFailureStage(stored_stage), reason)
                continue
            try:
                async with app.limits.io:
                    data = await app.hub.r2.get_object(m["asset_key"])
                blob = inspect_image(data)
                if blob is None:
                    raise ValueError("R2 final asset is not a readable image")
                bio = io.BytesIO(data)
                # Name + force_document=False causes Telethon to construct photo media, not
                # a URL/path message or anonymous application/octet-stream document.
                bio.name = f"{task_id[:8]}-{idx}.{blob.ext}"
                files.append((idx, bio))
            except Exception as exc:
                log.exception("R2 final asset fetch failed", extra=ctx(task_id=task_id, idx=idx))
                fail(idx, MediaFailureStage.R2_FETCH, tr("media_stage_fetch") + " " + media_error_reason(exc, locale))

        draft = task.get("draft_text") or ""
        caption_fits = len(draft.encode("utf-16-le")) // 2 <= 1024
        caption_sent = False
        anchor_id = None
        for offset in range(0, len(files), 10):
            batch = files[offset:offset + 10]
            try:
                # One photo uses sendMedia; albums contain 2–10 images. Put the draft on
                # the first photo whose caption delivery is confirmed, never on a status card.
                caption = draft if caption_fits and not caption_sent else ""
                file_arg = batch[0][1] if len(batch) == 1 else [bio for _, bio in batch]
                caption_arg = caption if len(batch) == 1 else [caption] + [""] * (len(batch)-1)
                kwargs = {"caption": caption_arg, "parse_mode": None, "force_document": False}
                if source_reply is not None:
                    kwargs["reply_to"] = source_reply
                if len(files) == 1:
                    # Telegram albums have no inline markup; single-photo bundles can carry controls.
                    kwargs["buttons"] = [[
                        Button.inline(tr("btn_rerun_images"), f"t:i:{task_id}".encode()),
                        Button.inline(tr("btn_image_tools"), f"it:{task_id}:v:main".encode()),
                    ]]
                sent = await self.client.send_file(chat_id, file_arg, **kwargs)
                messages = sent if isinstance(sent, (list, tuple)) else [sent]
                for pos, (idx, _) in enumerate(batch):
                    message = messages[pos] if pos < len(messages) else None
                    if message is None or getattr(message, "photo", None) is None:
                        fail(idx, MediaFailureStage.TELEGRAM_SEND, tr("media_stage_send"))
                    else:
                        message_id = getattr(message, "id", None)
                        delivery[str(idx)] = {"sent": True, "message_id": message_id}
                        if anchor_id is None:
                            anchor_id = message_id
                        if pos == 0 and caption:
                            caption_sent = True
                            delivery[str(idx)]["caption_sent"] = True
            except Exception as exc:
                log.exception("Telegram photo send failed", extra=ctx(task_id=task_id))
                for idx, _ in batch:
                    fail(idx, MediaFailureStage.TELEGRAM_SEND, tr("media_stage_send") + " " + media_error_reason(exc, locale))
        files.clear()
        for idx, (stage, reason) in failures.items():
            try:
                await app.repo.add_event(task_id, stage.value, f"image {idx}: {reason}", level="warning")
            except Exception:
                log.exception("could not record image delivery failure")
        if delivery:
            status = TaskStatus.NEEDS_REVIEW if failures or meta.get("problems") or meta.get("review") else TaskStatus.DRAFT_READY
            try:
                await app.repo.record_media_delivery(task_id, delivery, status)
            except Exception:
                log.exception("could not persist image delivery receipt")

        lines: list[str] = []
        problems = meta.get("problems") or []
        review = meta.get("review") or []
        if task["status"] == "needs_review" or failures:
            lines.append(tr("review_header", task_id=task_id))
            lines += [f"⛔ {esc(p)}" for p in problems]
            lines += [f"🔎 {esc(r)}" for r in review]
        else:
            lines.append(tr("draft_header", task_id=task_id, score=task.get("score"),
                                length=meta.get("x_length", "?")))
        jev = task.get("jev_result")
        if jev and isinstance(jev, dict) and jev.get("route"):
            val = jev.get("value")
            val_str = f"{val:.2f}" if isinstance(val, (int, float)) else str(val or "0.00")
            conf = jev.get("confidence") or "-"
            route_str = label(locale, str(jev.get("route") or ""))
            if locale == "zh-CN":
                lines.append(f"⚖️ Jev 初筛: {route_str}（评分: {val_str}，置信度: {conf}）")
            else:
                lines.append(f"⚖️ Jev triage: {route_str} (score: {val_str}, conf: {conf})")
        ev = task.get("evaluation")
        if ev and isinstance(ev, dict) and "suitable" in ev:
            val = ev.get("value_score")
            val_str = f"{val:.2f}" if isinstance(val, (int, float)) else str(val or "0.00")
            if locale == "zh-CN":
                lines.append(f"📊 评估: 价值 {val_str}" + (f"（{esc(ev.get('angle'))}）" if ev.get("angle") else ""))
            else:
                lines.append(f"📊 Evaluation: value {val_str}" + (f" ({esc(ev.get('angle'))})" if ev.get("angle") else ""))
        for w in meta.get("warnings") or []:
            lines.append(f"⚠️ {esc(w)}")
        if media:
            summary = ", ".join(
                f"#{m['idx']} {'❌' if m['idx'] in failures else _DECISION_ICON.get(m.get('decision') or '', '?')}"
                f"{label(locale, 'review' if m['idx'] in failures else m.get('decision') or '-')}" for m in media
            )
            lines.append(tr("media_summary", summary=esc(summary)))
            for m in media:
                idx = m["idx"]
                if idx in failures:
                    stage, reason = failures[idx]
                    display_stage = "IMAGE_QC" if stage == MediaFailureStage.QC else stage.value
                    clean_reason = reason.removeprefix(f"[{stage.value}] ")
                    lines.append(tr("media_stage_failure", idx=idx, stage=display_stage, reason=esc(clean_reason)))
                elif delivery.get(str(idx), {}).get("sent"):
                    lines.append(tr("media_delivery_sent", idx=idx))
                elif delivery.get(str(idx), {}).get("omitted"):
                    lines.append(tr("media_delivery_omitted", idx=idx))
            if any(m.get("ai_generated") for m in media):
                lines.append(tr("ai_label"))
        unavailable = [idx for idx, (stage, _) in failures.items()
                       if stage in (MediaFailureStage.R2_FETCH, MediaFailureStage.TELEGRAM_SEND)]
        if unavailable:
            lines.append(tr("media_unavailable", items=", ".join(f"#{i}" for i in sorted(unavailable))))
        lines.append(tr("draft_in_image_caption" if caption_sent else "draft_text_follows"))
        if any(m.get("decision") == "info_card" and tr("cleanup_card_fallback") in
               (m.get("decision_reason") or "") for m in media):
            lines.append(tr("cleanup_card_fallback"))

        buttons = []
        if task["status"] == "draft_ready" and not failures:
            buttons.append(Button.inline(tr("btn_approve"), f"t:a:{task_id}".encode()))
        elif (task["status"] == "needs_review" or failures) and not problems:
            buttons.append(Button.inline(tr("btn_approve_reviewed"),
                                         f"t:a:{task_id}".encode()))
        buttons += [Button.inline(tr("btn_regenerate"), f"t:g:{task_id}".encode()),
                    Button.inline(tr("btn_reject"), f"t:r:{task_id}".encode())]
        if meta.get("image_locale") and meta["image_locale"] != locale:
            lines.append(tr("image_target_locale", locale=meta["image_locale"]))
        image_button = Button.inline(tr("btn_image_tools"), f"it:{task_id}:v:main".encode())
        auth_buttons = [[Button.inline(tr("btn_confirm_edit_rights", idx=m["idx"]),
                                       f"ia:{task_id}:{m['idx']}".encode())]
                        for m in media if needs_edit_confirmation(task, m)]
        await self.update_task_status(task_id, "final", text="\n".join(lines), buttons=[buttons, *auth_buttons, [Button.inline(tr("btn_rerun_images"), f"t:i:{task_id}".encode()), image_button]], reply_to=anchor_id)
        if draft and not caption_sent:
            kwargs = {"parse_mode": None, "link_preview": False}
            if source_reply is not None or anchor_id is not None:
                kwargs["reply_to"] = source_reply if source_reply is not None else anchor_id
            await self.client.send_message(chat_id, draft, **kwargs)

    # ------------------------------------------------------------------ handlers

    def _authorized(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._allowed

    def _menu(self) -> list[list[Button]]:
        return [
            [Button.inline(self.t("btn_refresh_models"), b"m:refresh"),
             Button.inline(self.t("btn_test_connections"), b"m:health")],
            [Button.inline(self.t("btn_image_tools"), b"it:-:v:main")],
            [Button.inline(self.t("btn_recent"), b"m:recent"),
             Button.inline(self.t("btn_language"), b"m:language")],
        ]

    async def _on_command(self, event: events.NewMessage.Event) -> None:
        if not event.is_private:
            return
        if not self._authorized(event.sender_id):
            await event.respond(self.t("unauthorized"))
            return
        cmd = event.pattern_match.group(1)
        if cmd in ("start", "help"):
            await event.respond(esc(self.t("welcome")), buttons=self._menu())
        elif cmd in ("menu", "settings"):
            await event.respond(self.t("menu_title"), buttons=self._menu())
        elif cmd == "images":
            await ImageTools(self).open(event)
        elif cmd == "recent":
            await event.respond(await self._recent_text())

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        if not self._authorized(event.sender_id):
            await event.respond(self.t("unauthorized"))
            return
        # Manual mode preserves one task per message; automatic mode groups an explicit short burst.
        await self._receive(event, [event.message])

    async def _on_album(self, event: events.Album.Event) -> None:
        if not self._authorized(event.sender_id):
            return
        # Albums stay intact; opted-in automatic mode can combine a nearby text message with them.
        await self._receive(event, list(event.messages))

    async def _receive(self, event: Any, messages: list[Any]) -> None:
        # Serialize receipt creation and collector insertion so simultaneous album/text events
        # share one status message. The collector owns the first event's emit closure.
        if not hasattr(self, "_intake_lock"):
            self._intake_lock = asyncio.Lock()
        async with self._intake_lock:
            service = getattr(self.app, "image_preferences", None)
            preferences = await service.get(event.sender_id) if service else ImagePreferences()
            key = (event.chat_id, event.sender_id)
            collector = getattr(self, "_collector", None)
            automatic = preferences.workflow_mode == WorkflowMode.AUTO_BUNDLE
            if automatic and not getattr(self.app, "shutting_down", False):
                if collector is None:
                    collector = self._collector = BundleCollector()
                status_id = None if key in collector.pending else await self._receipt(event, messages)
                async def emit(bundle, snapshot):
                    await self._intake(event, bundle, preferences=snapshot,
                                       status_message_id=status_id)
                await collector.add(key, messages, preferences, emit)
            else:
                if collector:
                    await collector.flush(key)
                status_id = await self._receipt(event, messages)
                await self._intake(event, messages, preferences=preferences,
                                   status_message_id=status_id)

    async def _intake(self, event: Any, messages: list[Any], *, preferences: ImagePreferences | None = None,
                      status_message_id: int | None = None) -> None:
        try:
            kwargs = {}
            if preferences is not None:
                kwargs["preferences"] = preferences
            if status_message_id is not None:
                kwargs["status_message_id"] = status_message_id
            await self.app.intake(event.chat_id, event.sender_id, messages, **kwargs)
        except Exception:
            log.exception("intake failed")
            text = self.t("action_failed", result=self.t("generic_failure"))
            if status_message_id is not None:
                await self.client.edit_message(event.chat_id, status_message_id, text)
            else:
                await event.respond(text)

    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        if not self._authorized(event.sender_id):
            await event.answer(self.t("unauthorized"), alert=True)
            return
        data = event.data.decode(errors="ignore")
        ops = self.app.ops
        try:
            if data.startswith("ia:"):
                await event.answer()
                match = re.fullmatch(r"ia:([0-9a-f]{32}):([0-9]{1,3})", data)
                if not match:
                    await event.respond(self.t("edit_auth_stale"))
                    return
                _, message = await ops.authorize_image_edit(match[1], event.sender_id,
                                                           event.chat_id, int(match[2]))
                await event.respond(message)
            elif data.startswith("it:"):
                await ImageTools(self).handle(event, data)
            elif data == "m:menu":
                await event.answer()
                await event.edit(self.t("menu_title"), buttons=self._menu())
            elif data == "m:language":
                await event.answer()
                await event.respond(self.t("language_settings"), buttons=[[
                    Button.inline(("☑ " if self.app.config.current.default_locale == "en-US" else "☐ ") + "English (US)", b"m:locale:en-US"),
                    Button.inline(("☑ " if self.app.config.current.default_locale == "zh-CN" else "☐ ") + "简体中文", b"m:locale:zh-CN")]])
            elif data.startswith("m:locale:"):
                locale = data.removeprefix("m:locale:")
                if locale not in self.app.i18n.codes:
                    await event.answer(self.t("generic_failure"), alert=True)
                    return
                await self.app.config.update({"default_locale": locale})
                await event.answer(self.t("locale_saved"))
                await event.respond(self.t("menu_title"), buttons=self._menu())
            elif data == "m:refresh":
                await event.answer(self.t("working"))
                summary = await ops.refresh_models()
                await event.respond(self.t("models_refreshed", summary=esc(ops.model_summary(summary))))
            elif data == "m:health":
                await event.answer(self.t("working"))
                results = await ops.test_connections()
                lines = [self.t("health_title")] + [
                    f"{'✅' if r.ok else '❌'} <b>{esc(r.name)}</b> {r.latency_ms} "
                    f"{esc(self.t('admin_ms'))} — "
                    f"{esc(self.t('connection_ok' if r.ok else 'connection_failed'))}" for r in results
                ]
                await event.respond("\n".join(lines))
            elif data == "m:recent":
                await event.answer()
                await event.respond(await self._recent_text())
            elif data.startswith("t:"):
                _, action, task_id = data.split(":", 2)
                await self._task_action(event, action, task_id)
            else:
                await event.answer()
        except Exception:
            log.exception("callback failed", extra=ctx(data=data))
            await event.respond(self.t("action_failed", result=self.t("generic_failure")))

    async def _task_action(self, event: events.CallbackQuery.Event, action: str, task_id: str
                           ) -> None:
        ops = self.app.ops
        if action == "i":
            await event.answer()
            _, message = await ops.rerun_bundle_images(task_id, event.sender_id, event.chat_id)
            await event.respond(message)
        elif action == "a":
            ok, msg = await ops.approve(task_id)
            await event.answer(msg)
            if ok:
                task = await self.app.repo.get_task(task_id)
                await event.respond(self.t("approved", task_id=task_id))
                if task and task.get("draft_text"):
                    await self.client.send_message(event.chat_id, task["draft_text"],
                                                   parse_mode=None, link_preview=False)
            else:
                await event.respond(self.t("action_failed", result=esc(msg)))
        elif action == "r":
            ok, msg = await ops.reject(task_id)
            await event.answer(msg)
            await event.respond(self.t("rejected", task_id=task_id) if ok
                                else self.t("action_failed", result=esc(msg)))
        elif action == "g":
            ok, msg = await ops.regenerate(task_id)
            await event.answer(msg)
            await event.respond(self.t("regenerating", task_id=task_id, result=esc(msg)) if ok
                                else self.t("action_failed", result=esc(msg)))

    async def _recent_text(self) -> str:
        tasks = await self.app.repo.list_tasks(limit=10)
        if not tasks:
            return self.t("no_tasks")
        lines = [self.t("recent_title")]
        for t in tasks:
            preview = (t.get("preview") or "").replace("\n", " ")[:60]
            lines.append(f"<code>{t['id'][:8]}</code> {esc(label(self.app.config.current.default_locale, t['status']))} — {esc(preview)}")
        return "\n".join(lines)
