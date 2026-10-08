"""Small deterministic prefilter + per-message core extraction, before Jev.

Source envelopes remain immutable for audit/retries. Only the canonical bundle reaches models.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel

from .. import prompts
from ..models import InputEnvelope
from .guards import numbers_in

log = logging.getLogger(__name__)
_URL = re.compile(r"https?://[^\s<>]+")
_NOISE = re.compile(
    r"^\s*(?:forwarded from\b|转发自|轉發自|(?:join|follow|subscribe)(?:\s+(?:us|our|the|this|@))\b"
    r"|(?:加入|关注|關注|订阅|訂閱)(?:我们|我們|本|频道|頻道|群|@)"
    r"|(?:submission|contact|投稿|商务合作|商務合作|广告合作|廣告合作)\s*[:：]"
    r"|(?:TG|Telegram|频道|頻道)\s*[:：].*(?:@[A-Za-z0-9_]+|https?://(?:t\.me|telegram\.me)/))", re.I)
_HASHTAG = re.compile(r"(?<!\w)#\w+")


class CoreContent(BaseModel):
    core_text: str  # empty only when this message contains no publishing facts


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.hostname or '').lower() in {'t.me', 'telegram.me', 'telegram.dog'}:
        return ''
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid', 'ref', 'ref_src'}]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def preclean(text: str) -> str:
    seen_tags: set[str] = set()

    def tag(match: re.Match[str]) -> str:
        value = match[0].casefold()
        if value in seen_tags:
            return ''
        seen_tags.add(value)
        return match[0]

    lines = []
    for line in text.splitlines():
        if _NOISE.search(line):
            continue
        line = _URL.sub(lambda m: clean_url(m[0]), line)
        line = _HASHTAG.sub(tag, line).strip()
        if line:
            lines.append(line)
    return '\n'.join(lines)


async def clean_bundle(env: InputEnvelope, app) -> tuple[InputEnvelope, bool]:
    """Extract each message independently, deduplicate, then combine in original order.

    LLM failure or invented numbers uses the deterministic result and records a warning.
    Cancellation propagates, preserving worker timeout and graceful shutdown behavior.
    """
    if env.is_direct:
        # Operator directly typed or sent this message (not forwarded).
        # Preserve original text directly without stripping or LLM extraction loss.
        source = env.message_texts or ([env.text] if env.text else [])
        canonical = "\n\n".join(preclean(t) for t in source if t.strip()) or env.text
        urls = list(dict.fromkeys(_URL.findall(canonical) + env.urls))
        return env.model_copy(update={'text': canonical, 'message_texts': [canonical] if canonical else [], 'message_urls': [], 'urls': urls}), False

    cfg = app.config.current
    source = env.message_texts or ([env.text] if env.text else [])
    cores: list[str] = []
    fallback = False
    for idx, text in enumerate(source):
        # Hidden Telegram link entities are facts/sources too, never attribution metadata.
        links = env.message_urls[idx] if idx < len(env.message_urls) else []
        if not env.message_texts:  # legacy envelopes have no per-message URL mapping
            links = env.urls
        clean_links = [clean_url(url) for url in links]
        text_with_links = "\n".join([text, *(url for url in clean_links if url and url not in text)])
        cleaned = preclean(text_with_links)
        if not cleaned:
            continue
        p = prompts.render('extract_core', env.locale, text=cleaned)
        try:
            async with app.limits.text:
                result = await app.hub.cpa.chat_json(
                    cfg.models.text_model, p.messages(), CoreContent,
                    temperature=cfg.models.text_temperature, json_mode=cfg.models.json_mode)
            core = preclean(result.core_text.strip())
            if numbers_in(core) - numbers_in(cleaned):
                raise ValueError('extraction introduced numbers')
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning('core extraction failed; using deterministic cleaning')
            core, fallback = cleaned, True
        if core and core not in cores:
            cores.append(core)
    canonical = '\n\n'.join(cores)
    urls = list(dict.fromkeys(_URL.findall(canonical)))
    return env.model_copy(update={'text': canonical, 'message_texts': cores, 'message_urls': [], 'urls': urls}), fallback
