"""Turn Telethon messages (single, album, or a burst of forwards) into one InputEnvelope.

Uses duck typing on Telethon's Message so it can be unit-tested with simple fakes.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..models import ForwardOrigin, InputEnvelope, MediaKind, SourceMedia


def _media_of(msg: Any) -> SourceMedia | None:
    if getattr(msg, "photo", None) is not None:
        f = getattr(msg, "file", None)
        return SourceMedia(message_id=msg.id, kind=MediaKind.PHOTO, mime="image/jpeg",
                           size=getattr(f, "size", None))
    doc = getattr(msg, "document", None)
    if doc is None:
        return None
    f = getattr(msg, "file", None)
    mime = getattr(f, "mime_type", None) or getattr(doc, "mime_type", None)
    if getattr(msg, "sticker", None) is not None:
        return None
    if mime and mime.startswith("image/"):
        kind = MediaKind.IMAGE_DOCUMENT
    elif mime and mime.startswith("video/") or getattr(msg, "gif", None) is not None:
        kind = MediaKind.VIDEO
    else:
        kind = MediaKind.OTHER
    return SourceMedia(message_id=msg.id, kind=kind, mime=mime,
                       file_name=getattr(f, "name", None), size=getattr(f, "size", None))


def _peer_to_chat_id(peer: Any) -> int | None:
    if peer is None:
        return None
    try:
        from telethon import utils

        return utils.get_peer_id(peer)
    except Exception:
        for attr, sign in (("channel_id", "channel"), ("chat_id", "chat"), ("user_id", "user")):
            value = getattr(peer, attr, None)
            if value is not None:
                if sign == "channel":
                    return int(f"-100{value}")
                return -int(value) if sign == "chat" else int(value)
    return None


def _forward_of(msg: Any) -> ForwardOrigin | None:
    fwd = getattr(msg, "fwd_from", None)
    if fwd is None:
        return None
    return ForwardOrigin(
        chat_id=_peer_to_chat_id(getattr(fwd, "from_id", None)),
        sender_name=getattr(fwd, "from_name", None) or getattr(fwd, "post_author", None),
        post_id=getattr(fwd, "channel_post", None),
        date=getattr(fwd, "date", None),
    )


def _urls_of(msg: Any) -> list[str]:
    urls: list[str] = []
    getter = getattr(msg, "get_entities_text", None)
    if not callable(getter):
        return urls
    try:
        pairs = getter()
    except Exception:
        return urls
    for entity, text in pairs or []:
        url = getattr(entity, "url", None)  # MessageEntityTextUrl
        if url:
            urls.append(url)
        elif type(entity).__name__ == "MessageEntityUrl":
            urls.append(text)
    return urls


def normalize(messages: Iterable[Any], *, chat_id: int, user_id: int, locale: str,
              market: str) -> InputEnvelope:
    msgs = sorted((m for m in messages if m is not None), key=lambda m: m.id)
    texts: list[str] = []
    urls: list[str] = []
    media: list[SourceMedia] = []
    forwards: list[ForwardOrigin] = []
    grouped: list[int] = []
    for m in msgs:
        text = (getattr(m, "raw_text", None) or getattr(m, "message", None) or "").strip()
        if text and text not in texts:  # albums often repeat the caption
            texts.append(text)
        for u in _urls_of(m):
            if u not in urls:
                urls.append(u)
        item = _media_of(m)
        if item:
            media.append(item)
        fwd = _forward_of(m)
        if fwd and fwd not in forwards:
            forwards.append(fwd)
        gid = getattr(m, "grouped_id", None)
        if gid and gid not in grouped:
            grouped.append(gid)
    return InputEnvelope(
        chat_id=chat_id,
        user_id=user_id,
        message_ids=[m.id for m in msgs],
        grouped_ids=grouped,
        text="\n\n".join(texts),
        urls=urls,
        media=media,
        forwards=forwards,
        locale=locale,
        market=market,
    )
