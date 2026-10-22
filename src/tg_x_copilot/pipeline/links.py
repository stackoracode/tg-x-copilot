"""Pipeline step: read linked pages and add them to the task as extra source messages.

Runs in the worker (not in the web request), for both channels, before cleaning. Each page
becomes its own message so the per-message cleaning step treats it like a forwarded post.
The stored envelope is never changed: regenerate fetches again (a page that was blocked may
work on the next try).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..i18n import t
from ..models import InputEnvelope
from ..image_settings import ImageAction
from ..services.link_reader import LinkReader
from .cleaning import clean_url

_URL_ONLY_LIMIT = 300  # "short" Telegram posts: own text (links removed) under this many chars

EventFn = Callable[..., Awaitable[None]]


def links_to_read(env: InputEnvelope, web_cfg: Any) -> list[str]:
    """Which URLs this task should read (cleaned, deduplicated, capped)."""
    if env.channel == "web":
        if not (env.fetch_links and web_cfg.fetch_urls):
            return []
    else:
        mode = web_cfg.fetch_telegram_links
        if mode == "off":
            return []
        if mode == "short" and not env.fetch_links:
            own = env.text
            for url in env.urls:
                own = own.replace(url, "")
            if len(own.strip()) >= _URL_ONLY_LIMIT:
                return []  # the post carries its own content; links are references
    out: list[str] = []
    for url in env.urls:
        cleaned = clean_url(url)  # drops t.me links and tracking parameters
        if cleaned and cleaned.startswith(("http://", "https://")) and cleaned not in out:
            out.append(cleaned)
    return out[: max(0, web_cfg.fetch_max_links)]


async def attach_linked_pages(task_id: str, env: InputEnvelope, app: Any,
                              event: EventFn) -> tuple[InputEnvelope, list[str], bool]:
    """Return (envelope with page messages appended, operator-facing notes, any_ai_text)."""
    reader = getattr(app, "links", None)
    if env.image_action == ImageAction.MOBILE_TEXT and isinstance(reader, LinkReader):
        reader = reader.with_max_chars(app.config.current.mobile_text.source_max_chars)
    urls = links_to_read(env, app.config.current.web) if reader is not None else []
    if not urls:
        return env, [], False
    results = await asyncio.gather(*(reader.read(u, allow_ai=env.fetch_ai) for u in urls))
    texts = list(env.message_texts or ([env.text] if env.text else []))
    links = list(env.message_urls) + [[] for _ in range(len(texts) - len(env.message_urls))]
    notes: list[str] = []
    ai_text = False
    pages: list[str] = []
    for result in results:
        page = result.page
        await event(task_id, "links", f"{result.url}: {result.summary}",
                    level="info" if page else "warning",
                    data={"url": result.url, "method": page.method if page else None,
                          "chars": len(page.text) if page else 0})
        if page is None or not page.text.strip():
            notes.append(t(env.locale, "link_failed", url=result.url, reason=result.summary[:200]))
            continue
        pages.append(page.as_message())
        texts.append(pages[-1])
        links.append([result.url])
        if page.ai_retrieved:
            ai_text = True
            notes.append(t(env.locale, "link_ai_retrieved", url=result.url))
    if not pages:
        return env, notes, False
    return env.model_copy(update={
        "message_texts": texts, "message_urls": links,
        "text": "\n\n".join(x for x in [env.text, *pages] if x.strip()),
    }), notes, ai_text
