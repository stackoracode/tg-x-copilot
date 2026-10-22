"""Fetch readable text from a URL (web compose page, Telegram links, /fetch, link preview).

`fetch_page` is the plain HTTP fetch. `LinkReader` (link_reader.py) layers fallbacks on top
(reader service, headless browser, LLM with web access) for pages that block simple clients.

SSRF guard (the server fetches URLs typed by the operator):
- only http/https, no credentials in the URL;
- every hop (initial URL and each redirect, max 4) must resolve exclusively to public
  (globally routable) addresses: loopback, private, link-local, CGNAT, metadata and reserved
  ranges are refused;
- the body is streamed and capped (`web.fetch_max_bytes`), the whole fetch is time-limited.
Residual risk: DNS rebinding between our check and the connection. The fetch only ever reads
text into the task, and the page is behind admin auth, which keeps the impact small.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

Resolver = Callable[[str, int], Awaitable[list[str]]]

_MAX_REDIRECTS = 4
_TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg",
         "iframe", "template", "button", "select"}
_WS = re.compile(r"\s+")
_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_-]+)", re.I)
# Desktop browser headers: many sites serve a block / "unsupported client" page to bot UAs.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}
# Second try: a phone (WeChat's in-app browser for mp.weixin.qq.com articles).
MOBILE_HEADERS = {
    **BROWSER_HEADERS,
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50(0x18003231) "
                   "NetType/WIFI Language/zh_CN"),
}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
          "tr", "section", "article", "figcaption", "dd", "dt", "td"}


class UrlFetchError(Exception):
    pass


@dataclass(frozen=True)
class PageContent:
    url: str
    title: str
    text: str
    method: str = "direct"  # direct | reader | browser | llm

    @property
    def ai_retrieved(self) -> bool:
        """Text produced by a model's own browsing: unverified, may be incomplete."""
        return self.method == "llm"

    def as_message(self) -> str:
        head = self.title.strip()
        return f"{head}\n\n{self.text}".strip() if head and head not in self.text[:200] else self.text


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.in_title = False
        self.title = ""
        self.meta: dict[str, str] = {}
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:title", "og:description", "description") and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self.in_title = False
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        elif not self.skip_depth:
            # HTML whitespace (incl. source newlines) collapses; only block tags break lines.
            self.parts.append(_WS.sub(" ", data))


def extract_text(html: str, *, max_chars: int = 20000) -> tuple[str, str]:
    """Return (title, readable text). Pure; tested without network."""
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    title = " ".join((parser.meta.get("og:title") or parser.title).split())
    lines: list[str] = []
    for raw in "".join(parser.parts).splitlines():
        line = " ".join(raw.split())
        if len(line) >= 3 and (not lines or lines[-1] != line):
            lines.append(line)
    description = parser.meta.get("og:description") or parser.meta.get("description") or ""
    if description and description not in lines[:5]:
        lines.insert(0, " ".join(description.split()))
    return title, "\n".join(lines)[:max_chars]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def check_url(url: str, resolver: Resolver = _system_resolver) -> str:
    """Validate scheme/host and that every resolved address is public. Returns the URL."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UrlFetchError("only http(s) URLs with a host are allowed")
    if parts.username or parts.password:
        raise UrlFetchError("URLs with credentials are not allowed")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(parts.hostname)
        addresses = [str(literal)]
    except ValueError:
        try:
            addresses = await resolver(parts.hostname, port)
        except OSError as exc:
            raise UrlFetchError(f"cannot resolve host: {exc}") from exc
    if not addresses or not all(_is_public(a) for a in addresses):
        raise UrlFetchError("host resolves to a non-public address")
    return url.strip()


def _decode(body: bytes, header_encoding: str | None) -> str:
    """Charset from the Content-Type header, else <meta charset> (GBK/Big5 news sites)."""
    encoding = header_encoding
    if not encoding:
        match = _META_CHARSET.search(body[:4096])
        encoding = match[1].decode("ascii") if match else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def fetch_page(http: httpx.AsyncClient, url: str, *, timeout: float, max_bytes: int,
                     max_chars: int, resolver: Resolver = _system_resolver,
                     headers: dict[str, str] | None = None) -> PageContent:
    async def run() -> PageContent:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            await check_url(current, resolver)
            async with http.stream("GET", current, follow_redirects=False,
                                   headers=headers or BROWSER_HEADERS) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                if resp.status_code >= 400:
                    raise UrlFetchError(f"HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype and ctype not in _TEXT_TYPES:
                    raise UrlFetchError(f"unsupported content type {ctype}")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        break  # keep what we have: the readable start of a long page
                charset = resp.charset_encoding
            text = _decode(bytes(body[:max_bytes]), charset)
            if ctype == "text/plain":
                return PageContent(current, "", text[:max_chars].strip())
            title, readable = extract_text(text, max_chars=max_chars)
            return PageContent(current, title, readable)
        raise UrlFetchError("too many redirects")

    try:
        return await asyncio.wait_for(run(), timeout=timeout)
    except TimeoutError as exc:
        raise UrlFetchError(f"timed out after {timeout:.0f}s") from exc
    except httpx.HTTPError as exc:
        raise UrlFetchError(f"{type(exc).__name__}") from exc


URL_RE = re.compile(r"https?://[^\s<>\"']+")


# Phrases of anti-bot / verification interstitials (WeChat "环境异常", Cloudflare, captchas).
_BLOCK_MARKERS = (
    "环境异常", "完成验证后即可继续访问", "请在微信客户端打开链接", "访问过于频繁", "请输入验证码",
    "安全验证", "人机验证", "滑动验证", "Just a moment...", "Attention Required!",
    "Checking your browser", "Enable JavaScript and cookies to continue", "verify you are human",
    "Verifying you are human", "unusual traffic", "Access Denied", "Access denied",
    "Please enable JavaScript", "Please turn JavaScript on", "captcha", "CAPTCHA",
)
MIN_TEXT_CHARS = 200  # less than this is a stub (JS shell, cookie wall) worth another strategy


def block_reason(page: PageContent) -> str | None:
    """Why `page` is not the real article: a verification/anti-bot page or (almost) no text.
    Markers only count on short pages or in the title, so articles *about* captchas pass."""
    text = page.text.strip()
    for marker in _BLOCK_MARKERS:
        if marker in page.title or (marker in text and len(text) < 1500):
            return f"blocked ({marker})"
    if len(text) < MIN_TEXT_CHARS:
        return f"too little text ({len(text)} chars)"
    return None
