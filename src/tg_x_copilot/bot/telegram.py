"""Telethon bot: transport only. Receives forwards, shows drafts, exposes ops buttons.

Business logic lives in the pipeline/services; this module only translates between Telegram
and them. All outgoing text uses HTML parse mode with escaped dynamic content.
"""

from __future__ import annotations

import html
import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telethon import Button, TelegramClient, events

from ..i18n import label
from .image_tools import ImageTools
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

    async def stop(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()
        log.info("telegram bot disconnected")

    # ------------------------------------------------------------------ helpers used by pipeline

    async def notify(self, chat_id: int, text: str, buttons: Any = None) -> None:
        await self.client.send_message(chat_id, text[:_MAX_MSG], buttons=buttons,
                                       link_preview=False)

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

    async def send_draft(self, task_id: str) -> None:
        app = self.app
        task = await app.repo.get_task(task_id)
        if not task:
            return
        def tr(key: str, **kw: Any) -> str:
            return app.i18n.t(task.get("locale", app.config.current.default_locale), key, **kw)
        locale = task.get("locale", app.config.current.default_locale)
        chat_id = task["tg_chat_id"]
        meta = task.get("draft_meta") or {}
        media = await app.repo.list_media(task_id)

        # Images are best-effort: an R2 or Telegram media failure never blocks the text draft.
        files: list[io.BytesIO] = []
        unavailable: list[int] = []
        for m in media:
            if m.get("asset_key") and m.get("asset_kind") == "final":
                try:
                    async with app.limits.io:
                        data = await app.hub.r2.get_object(m["asset_key"])
                except Exception:
                    log.warning("asset fetch failed; sending draft without it",
                                extra=ctx(task_id=task_id, idx=m["idx"]))
                    unavailable.append(m["idx"])
                    continue
                bio = io.BytesIO(data)
                bio.name = f"{task_id[:8]}-{m['idx']}{Path(m['asset_key']).suffix}"
                files.append(bio)
        for offset in range(0, len(files), 10):
            batch = files[offset:offset + 10]
            try:
                await self.client.send_file(chat_id, batch)
            except Exception:
                log.warning("sending images failed; sending draft text anyway",
                            extra=ctx(task_id=task_id))
                unavailable += [int(f.name.split("-")[-1].split(".")[0]) for f in batch]
        files.clear()

        lines: list[str] = []
        problems = meta.get("problems") or []
        review = meta.get("review") or []
        if task["status"] == "needs_review":
            lines.append(tr("review_header", task_id=task_id))
            lines += [f"⛔ {esc(p)}" for p in problems]
            lines += [f"🔎 {esc(r)}" for r in review]
        else:
            lines.append(tr("draft_header", task_id=task_id, score=task.get("score"),
                                length=meta.get("x_length", "?")))
        for w in meta.get("warnings") or []:
            lines.append(f"⚠️ {esc(w)}")
        if media:
            summary = ", ".join(
                f"#{m['idx']} {_DECISION_ICON.get(m.get('decision') or '', '?')}"
                f"{label(locale, m.get('decision') or '-')}" for m in media
            )
            lines.append(tr("media_summary", summary=esc(summary)))
            for m in media:
                if m.get("decision") == "review":
                    lines.append(f"  #{m['idx']}: {esc(m.get('decision_reason') or '')}")
            if any(m.get("ai_generated") for m in media):
                lines.append(tr("ai_label"))
        if unavailable:
            lines.append(tr("media_unavailable",
                                items=", ".join(f"#{i}" for i in sorted(set(unavailable)))))
        lines.append(tr("draft_text_follows"))

        buttons = []
        if task["status"] == "draft_ready":
            buttons.append(Button.inline(tr("btn_approve"), f"t:a:{task_id}".encode()))
        elif task["status"] == "needs_review" and not problems:
            buttons.append(Button.inline(tr("btn_approve_reviewed"),
                                         f"t:a:{task_id}".encode()))
        buttons += [Button.inline(tr("btn_regenerate"), f"t:g:{task_id}".encode()),
                    Button.inline(tr("btn_reject"), f"t:r:{task_id}".encode())]
        if meta.get("image_locale") and meta["image_locale"] != locale:
            lines.append(tr("image_target_locale", locale=meta["image_locale"]))
        image_button = Button.inline(tr("btn_image_tools"), f"it:{task_id}:v:main".encode())
        await self.notify(chat_id, "\n".join(lines), buttons=[buttons, [image_button]])
        if task.get("draft_text"):
            await self.client.send_message(chat_id, task["draft_text"], parse_mode=None,
                                           link_preview=False)

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
        # Each regular (non-album) message is its own task; nothing is merged by arrival time.
        await self._intake(event, [event.message])

    async def _on_album(self, event: events.Album.Event) -> None:
        if not self._authorized(event.sender_id):
            return
        # Telegram albums (one grouped_id) are the only automatic grouping.
        await self._intake(event, list(event.messages))

    async def _intake(self, event: Any, messages: list[Any]) -> None:
        try:
            await self.app.intake(event.chat_id, event.sender_id, messages)
        except Exception:
            log.exception("intake failed")
            await event.respond(self.t("action_failed", result=self.t("generic_failure")))

    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        if not self._authorized(event.sender_id):
            await event.answer(self.t("unauthorized"), alert=True)
            return
        data = event.data.decode(errors="ignore")
        ops = self.app.ops
        try:
            if data.startswith("it:"):
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
        if action == "a":
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
