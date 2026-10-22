"""CSRF protection for the admin/web UI.

HTTP Basic credentials are re-sent by browsers automatically, so a cross-site form could drive
state-changing routes while the operator is logged in. Every unsafe request must therefore come
from the same origin: `Origin` (or, when absent, `Referer`) must match the request's own host or
one of `ADMIN__TRUSTED_ORIGINS` (for reverse proxies that rewrite the Host header). Requests
without either header are non-browser clients, which must send credentials explicitly anyway.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _netloc(url: str) -> str:
    return urlsplit(url).netloc.lower()


def is_same_origin(method: str, headers: dict[str, str], trusted: list[str]) -> bool:
    """Pure decision used by the dependency (header names lower-case)."""
    if method.upper() in SAFE_METHODS:
        return True
    source = headers.get("origin")
    if source is None:
        source = headers.get("referer")
    if source is None:
        return True  # not a browser form/fetch; credentials were sent explicitly
    if source.strip().lower() == "null":
        return False  # sandboxed/opaque origins are never trusted
    allowed = {h.lower() for h in (headers.get("host"), headers.get("x-forwarded-host")) if h}
    allowed |= {_netloc(o) for o in trusted if o}
    return _netloc(source) in allowed


def require_same_origin(request: Request) -> None:
    trusted = request.app.state.ctx.config.base.admin.trusted_origins
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not is_same_origin(request.method, headers, trusted):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site request blocked")
