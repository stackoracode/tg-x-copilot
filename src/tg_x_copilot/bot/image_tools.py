"""Registry-driven inline image tools. Callback payloads contain identifiers, never preferences."""

from __future__ import annotations

import html
import re

from telethon import Button
from telethon.errors import MessageNotModifiedError

from ..image_settings import (
    ACTIONS,
    PRESETS,
    ImageAction,
    ImageOption,
    ImageOptions,
    ImagePreferences,
    InformationDensity,
)

from ..pipeline.image_policy import needs_edit_confirmation

_SCOPE = re.compile(r"(?:-|[0-9a-f]{32})\Z")


def payload(scope: str, command: str, value: str = "") -> bytes:
    data = f"it:{scope}:{command}:{value}".encode()
    if len(data) > 64:
        raise ValueError("image callback exceeds Telegram limit")
    return data


def keyboard(scope: str, prefs: ImagePreferences, tr, page: str = "main"):
    def button(key, command, value="", selected=False):
        return Button.inline(
            ("☑ " if selected else "☐ ") + tr(key), payload(scope, command, value)
        )

    rows = []
    if page == "options":
        for option in ImageOption:
            enabled = option in prefs.image_options.flags
            rows.append(
                [
                    button(
                        "image_option_" + option.value,
                        "o",
                        option.value + (",0" if enabled else ",1"),
                        enabled,
                    )
                ]
            )
    elif page == "presets":
        for action in PRESETS:
            rows.append(
                [
                    button(
                        "image_action_" + action.value,
                        "p",
                        action.value,
                        prefs.image_action == action,
                    )
                ]
            )
    elif page == "density":
        rows = [
            [
                button(
                    "density_" + value.value,
                    "d",
                    value.value,
                    prefs.image_options.information_density == value,
                )
            ]
            for value in InformationDensity
        ]
    elif page == "locale":
        for value, name in (
            (None, tr("image_tools_follow")),
            ("en-US", "English (US)"),
            ("zh-CN", "简体中文"),
        ):
            rows.append(
                [
                    Button.inline(
                        ("☑ " if prefs.image_options.target_locale == value else "☐ ")
                        + name,
                        payload(scope, "l", value or "follow"),
                    )
                ]
            )
    else:
        rows = [[button("image_tools_auto", "a", "auto", prefs.image_action is None)]]
        choices = [
            button(
                "image_action_" + action.value,
                "a",
                action.value,
                prefs.image_action == action,
            )
            for action in ACTIONS
        ]
        rows += [choices[idx : idx + 2] for idx in range(0, len(choices), 2)]
        rows += [
            [
                Button.inline(
                    tr("image_tools_presets"), payload(scope, "v", "presets")
                ),
                Button.inline(
                    tr("image_tools_options"), payload(scope, "v", "options")
                ),
            ],
            [
                Button.inline(
                    tr(
                        "image_tools_density",
                        density=tr(
                            "density_" + prefs.image_options.information_density.value
                        ),
                    ),
                    payload(scope, "v", "density"),
                )
            ],
            [
                Button.inline(
                    tr(
                        "image_tools_locale",
                        locale=prefs.image_options.target_locale
                        or tr("image_tools_follow"),
                    ),
                    payload(scope, "v", "locale"),
                )
            ],
        ]
        if scope != "-":
            rows.append([Button.inline(tr("image_tools_run"), payload(scope, "run"))])
    rows.append(
        [
            Button.inline(
                tr("image_tools_back"),
                payload(scope, "v", "main") if page != "main" else b"m:menu",
            )
        ]
    )
    return rows


class ImageTools:
    def __init__(self, bot) -> None:
        self.bot = bot

    async def handle(self, event, data: str) -> None:
        # Stop Telegram's spinner before any DB work; explicit desired flag values are idempotent.
        await event.answer()
        _, scope, command, value = data.split(":", 3)
        if not _SCOPE.fullmatch(scope):
            raise ValueError("invalid task scope")
        app, tr = self.bot.app, self.bot.t
        if scope != "-":
            task = await app.repo.get_task(scope)
            if (
                not task
                or task.get("tg_user_id") != event.sender_id
                or task.get("tg_chat_id") != event.chat_id
            ):
                await event.respond(tr("image_tools_owner"))
                return
        page = "main"
        service = app.image_preferences
        if command == "run":
            prefs = await service.get(event.sender_id)
            ok, message = await app.ops.rerun_images(scope, event.sender_id, prefs)
            await event.respond(message)
            return
        if command in ("a", "p", "o", "d", "l"):

            def mutate(prefs):
                values = prefs.model_dump()
                opts = prefs.image_options
                if command == "a":
                    values["image_action"] = (
                        None if value == "auto" else ImageAction(value)
                    )
                elif command == "p":
                    chosen = ImageAction(value)
                    values["image_action"] = chosen
                    values["image_options"] = ImageOptions(
                        **{**opts.model_dump(), "flags": PRESETS[chosen]}
                    )
                elif command == "o":
                    name, desired = value.split(",")
                    if desired not in ("0", "1"):
                        raise ValueError("invalid option state")
                    values["image_options"] = opts.set_flag(
                        ImageOption(name), desired == "1"
                    )
                elif command == "d":
                    values["image_options"] = ImageOptions(
                        **{
                            **opts.model_dump(),
                            "information_density": InformationDensity(value),
                        }
                    )
                elif command == "l":
                    values["image_options"] = ImageOptions(
                        **{
                            **opts.model_dump(),
                            "target_locale": None if value == "follow" else value,
                        }
                    )
                return ImagePreferences(**values)

            prefs = await service.update(event.sender_id, mutate)
            page = {"o": "options", "d": "density", "l": "locale"}.get(command, "main")
        elif command == "v" and value in (
            "main",
            "options",
            "presets",
            "density",
            "locale",
        ):
            prefs = await service.get(event.sender_id)
            page = value
        else:
            raise ValueError("invalid image tools callback")
        text = (
            tr("image_tools_title")
            + "\n"
            + tr(
                "image_tools_action",
                action=(
                    tr("image_action_" + prefs.image_action.value)
                    if prefs.image_action
                    else tr("image_tools_auto")
                ),
            )
        )
        if (prefs.image_action and ACTIONS[prefs.image_action].text_capable and
            {ImageOption.MINIMAL_CHANGES, ImageOption.REMOVE_OVERLAYS} & prefs.image_options.flags):
            text += "\n" + tr("image_tools_creation_priority")
        buttons = keyboard(scope, prefs, tr, page)
        if scope != "-" and page == "main":
            confirm = [[Button.inline(app.i18n.t(task.get("locale"), "btn_confirm_edit_rights", idx=m["idx"]),
                                      f"ia:{scope}:{m['idx']}".encode())]
                       for m in await app.repo.list_media(scope) if needs_edit_confirmation(task, m)]
            buttons[-1:-1] = confirm  # Existing cards can reach confirmation without re-running anything.
        try:
            await event.edit(
                html.escape(text),
                buttons=buttons,
                link_preview=False,
            )
        except MessageNotModifiedError:
            pass

    async def open(self, event, scope: str = "-") -> None:
        prefs = await self.bot.app.image_preferences.get(event.sender_id)
        await event.respond(
            self.bot.t("image_tools_title"), buttons=keyboard(scope, prefs, self.bot.t)
        )
