"""URL boundaries in prose, shared by intake and cleaning; no network access."""

from __future__ import annotations

import re
from urllib.parse import SplitResult, urlsplit

# Chinese punctuation terminates a prose URL; Unicode letters remain valid in paths.
# ASCII parentheses can be part of a real URL (e.g. Wikipedia titles and DOI paths).
URL_RE = re.compile(r"https?://[^\s<>\"'`，。；！？：（）【】《》〈〉「」『』、“”‘’]+", re.IGNORECASE)


def trim_url_token(token: str) -> str:
    """Remove sentence punctuation and unmatched closing Markdown/prose brackets."""
    token = token.rstrip(".,;:!?")
    while token and token[-1] in ")]}":
        closer = token[-1]
        opener = {")": "(", "]": "[", "}": "{"}[closer]
        if token.count(closer) <= token.count(opener):
            break
        token = token[:-1].rstrip(".,;:!?")
    return token


def parse_http_url(url: str) -> SplitResult | None:
    """Syntactic validation only; malformed Unicode, IPv6 and ports never escape as errors."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        _ = parts.port  # Accessing this property validates the port.
        return parts
    except ValueError:
        return None


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in URL_RE.finditer(text):
        token = trim_url_token(match[0])
        if parse_http_url(token) is not None and token not in urls:
            urls.append(token)
    return urls
