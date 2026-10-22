"""Read a linked page through several strategies until one returns the real article.

Many pages refuse simple HTTP clients: WeChat articles answer datacenter IPs with "环境异常"
(environment exception), Cloudflare shows "Just a moment...", SPAs ship an empty shell.
Strategies, tried in `web.fetch_strategies` order:

    direct   plain HTTP with desktop-browser headers, then again as a phone / WeChat browser
    reader   a reader service (default r.jina.ai) fetches and renders the page from its own
             network; the URL is sent to that third party (set web.fetch_reader_url="" to stop)
    browser  local headless Chromium via Playwright (optional: `pip install playwright`, then
             `playwright install chromium`, or point web.fetch_browser_path at Chrome/Chromium)
    llm      a model with web access (web.fetch_llm_model) reads the page; its text is marked
             AI-retrieved because it cannot be verified byte-for-byte

A result counts only if `block_reason()` finds no verification page and enough text; otherwise
the next strategy runs. A thin-but-clean page (short post) is returned when nothing better
turns up. Every strategy keeps the SSRF rule: only public hosts (browser: every request).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel

from .. import prompts
from ..logging_setup import ctx as log_ctx
from ..logging_setup import mask
from .url_fetch import (
    MOBILE_HEADERS,
    PageContent,
    Resolver,
    UrlFetchError,
    _system_resolver,
    block_reason,
    check_url,
    extract_text,
    fetch_page,
)

log = logging.getLogger(__name__)

STRATEGIES = ("direct", "reader", "browser", "llm")
_SYSTEM_BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
                    "microsoft-edge")
_SKIP_RESOURCES = {"image", "media", "font"}
_BLANK_RUNS = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class Attempt:
    method: str
    ok: bool
    detail: str


@dataclass
class ReadResult:
    url: str
    page: PageContent | None = None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """'direct: blocked (环境异常); reader: ok' — for events, /fetch and the preview."""
        return "; ".join(f"{a.method}: {a.detail}" for a in self.attempts)

    def as_json(self) -> dict[str, Any]:
        page = self.page
        return {
            "url": self.url, "ok": page is not None,
            "method": page.method if page else None,
            "ai_retrieved": bool(page and page.ai_retrieved),
            "title": page.title if page else "",
            "chars": len(page.text) if page else 0,
            "excerpt": page.text[:400] if page else "",
            "attempts": [{"method": a.method, "ok": a.ok, "detail": a.detail}
                         for a in self.attempts],
        }


class _LlmPage(BaseModel):
    ok: bool
    title: str = ""
    text: str = ""


class LinkReader:
    """Stateless apart from a browser semaphore; reads settings and clients at call time so
    admin-UI changes apply immediately."""

    def __init__(self, app: Any, *, resolver: Resolver = _system_resolver) -> None:
        self.app = app
        self.resolver = resolver
        self._browser_slot = asyncio.Semaphore(1)  # one Chromium at a time on a small VPS

    @property
    def _cfg(self):
        return self.app.config.current.web

    def available(self, allow_ai: bool = True) -> list[str]:
        """Configured strategies that can run right now, in order."""
        cfg = self._cfg
        out = []
        for name in cfg.fetch_strategies:
            if name == "reader" and not cfg.fetch_reader_url:
                continue
            if name == "llm" and (not allow_ai or not cfg.fetch_llm_model):
                continue
            out.append(name)
        return out

    async def read(self, url: str, *, allow_ai: bool = True) -> ReadResult:
        result = ReadResult(url=url)
        try:
            await check_url(url, self.resolver)
        except UrlFetchError as exc:  # never hand an internal URL to any strategy
            result.attempts.append(Attempt("check", False, str(exc)))
            return result
        fallback: PageContent | None = None
        for name in self.available(allow_ai):
            started = time.monotonic()
            try:
                page = await getattr(self, f"_{name}")(url)
            except asyncio.CancelledError:
                raise
            except UrlFetchError as exc:
                result.attempts.append(Attempt(name, False, str(exc)[:200]))
                continue
            except Exception as exc:  # a broken strategy never stops the next one
                log.warning("link strategy failed", extra=log_ctx(method=name, error=mask(str(exc))))
                result.attempts.append(Attempt(name, False, f"{type(exc).__name__}"))
                continue
            took = f"{time.monotonic() - started:.1f}s"
            reason = block_reason(page)
            if reason is None:
                result.attempts.append(Attempt(name, True, f"ok, {len(page.text)} chars, {took}"))
                result.page = page
                return result
            result.attempts.append(Attempt(name, False, reason))
            if reason.startswith("too little") and page.text.strip() and (
                    fallback is None or len(page.text) > len(fallback.text)):
                fallback = page  # a genuinely short page (a post, a notice) is still content
        if not result.attempts:
            result.attempts.append(Attempt("none", False, "no fetch strategy enabled"))
        result.page = fallback
        return result

    # ------------------------------------------------------------------ strategies

    async def _direct(self, url: str) -> PageContent:
        cfg = self._cfg
        kwargs = dict(timeout=cfg.fetch_timeout_seconds, max_bytes=cfg.fetch_max_bytes,
                      max_chars=cfg.fetch_max_chars, resolver=self.resolver)
        http = self.app.hub.http
        first: PageContent | None = None
        try:
            first = await fetch_page(http, url, **kwargs)
            if block_reason(first) is None:
                return first
        except UrlFetchError as exc:
            if "content type" in str(exc) or "non-public" in str(exc):
                raise  # a phone won't change either
        # Same request as a phone / WeChat in-app browser: many CN sites (and WeChat) only
        # block desktop or unknown clients.
        try:
            second = await fetch_page(http, url, headers=MOBILE_HEADERS, **kwargs)
        except UrlFetchError:
            if first is not None:
                return first
            raise
        candidates = [p for p in (second, first) if p is not None]
        clean = [p for p in candidates if block_reason(p) is None]
        if clean:
            return clean[0]
        thin = [p for p in candidates if (block_reason(p) or "").startswith("too little")]
        return max(thin, key=lambda p: len(p.text)) if thin else second

    async def _reader(self, url: str) -> PageContent:
        cfg = self._cfg
        endpoint = cfg.fetch_reader_url.rstrip("/") + "/" + url
        headers = {"Accept": "application/json", "X-Return-Format": "text"}
        key = cfg.fetch_reader_api_key.get_secret_value()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            resp = await asyncio.wait_for(
                self.app.hub.http.get(endpoint, headers=headers,
                                      timeout=max(cfg.fetch_timeout_seconds, 30.0)),
                timeout=max(cfg.fetch_timeout_seconds, 30.0) + 5)
        except TimeoutError as exc:
            raise UrlFetchError("reader timed out") from exc
        except Exception as exc:
            raise UrlFetchError(f"reader unreachable ({type(exc).__name__})") from exc
        if resp.status_code >= 400:
            hint = ""
            if resp.status_code in (401, 402, 403) and not key:
                hint = " (needs an API key: WEB__FETCH_READER_API_KEY)"
            raise UrlFetchError(f"reader HTTP {resp.status_code}{hint}")
        body = resp.content[: cfg.fetch_max_bytes]
        title, text = "", ""
        try:
            data = json.loads(body).get("data") or {}
            title, text = str(data.get("title") or ""), str(data.get("content") or data.get("text") or "")
        except (ValueError, AttributeError):
            text = body.decode("utf-8", errors="replace")
        # Reader output is markdown-ish with long runs of blank lines (menus, spacing).
        text = "\n".join(line.strip() for line in text.splitlines())
        text = _BLANK_RUNS.sub("\n\n", text).strip()
        return PageContent(url, " ".join(title.split()), text[: cfg.fetch_max_chars],
                           method="reader")

    def _browser_executable(self) -> str | None:
        configured = self._cfg.fetch_browser_path
        if configured:
            return configured
        return next((p for p in map(shutil.which, _SYSTEM_BROWSERS) if p), None)

    async def _browser(self, url: str) -> PageContent:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise UrlFetchError("browser not installed (pip install playwright)") from exc
        cfg = self._cfg
        timeout_ms = int(cfg.fetch_browser_timeout_seconds * 1000)
        allowed: dict[str, bool] = {}

        async def guard(route: Any) -> None:
            request = route.request
            parts = urlsplit(request.url)
            if parts.scheme in ("data", "blob", "about"):
                await route.continue_()
                return
            if request.resource_type in _SKIP_RESOURCES or parts.scheme not in ("http", "https"):
                await route.abort()
                return
            origin = f"{parts.scheme}://{parts.netloc}"
            if origin not in allowed:  # SSRF: every request the page makes, not just the URL
                try:
                    await check_url(origin + "/", self.resolver)
                    allowed[origin] = True
                except UrlFetchError:
                    allowed[origin] = False
            await (route.continue_() if allowed[origin] else route.abort())

        async with self._browser_slot:
            async with async_playwright() as pw:
                launch: dict[str, Any] = {"headless": True}
                executable = self._browser_executable()
                try:
                    browser = await pw.chromium.launch(**launch)
                except Exception:
                    if not executable:
                        raise UrlFetchError("no Chromium found (playwright install chromium, "
                                            "or set web.fetch_browser_path)") from None
                    browser = await pw.chromium.launch(executable_path=executable, **launch)
                try:
                    context = await browser.new_context(
                        user_agent=MOBILE_HEADERS["User-Agent"] if "weixin.qq.com" in url
                        else None, locale="zh-CN", service_workers="block",
                        accept_downloads=False)
                    await context.route("**/*", guard)
                    page = await context.new_page()
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    except Exception as exc:
                        raise UrlFetchError(f"browser could not load the page "
                                            f"({type(exc).__name__})") from exc
                    try:  # let client-side rendering finish, but never wait forever
                        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
                    except Exception:
                        pass
                    html = await page.content()
                    final_url = page.url
                finally:
                    await browser.close()
        title, text = extract_text(html, max_chars=cfg.fetch_max_chars)
        return PageContent(final_url, title, text, method="browser")

    async def _llm(self, url: str) -> PageContent:
        cfg, app = self.app.config.current, self.app
        p = prompts.render("fetch_url", cfg.default_locale, url=url)
        try:
            async with app.limits.text:
                result = await asyncio.wait_for(
                    app.hub.cpa.chat_json(cfg.web.fetch_llm_model, p.messages(), _LlmPage,
                                          json_mode=cfg.models.json_mode),
                    timeout=max(cfg.web.fetch_timeout_seconds, 60.0))
        except TimeoutError as exc:
            raise UrlFetchError("AI retrieval timed out") from exc
        if not result.ok or not result.text.strip():
            raise UrlFetchError("AI model could not open the page")
        return PageContent(url, " ".join(result.title.split()),
                           result.text.strip()[: cfg.web.fetch_max_chars], method="llm")
