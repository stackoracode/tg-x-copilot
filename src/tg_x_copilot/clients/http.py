"""Shared httpx client + retry helper used by every upstream client."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from ..logging_setup import ctx, mask

log = logging.getLogger(__name__)

# 529 = TypeSafe "Overloaded" (docs.typesafe.ai/api: retry 429/529 with exponential backoff)
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


class UpstreamError(Exception):
    def __init__(self, service: str, status: int | None, detail: str) -> None:
        self.service = service
        self.status = status
        self.detail = mask(detail[:800])
        super().__init__(f"{service} error status={status}: {self.detail}")


def make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        follow_redirects=True,
        headers={"User-Agent": "tg-x-copilot/0.1"},
    )


def _retry_after(resp: httpx.Response | None) -> float | None:
    if resp is None:
        return None
    raw = resp.headers.get("retry-after")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    max_retries: int = 3,
    timeout: float | None = None,
    base_delay: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    """Retries transport errors and retryable status codes with jittered exponential backoff.

    Raises UpstreamError for non-retryable HTTP errors or when retries are exhausted.
    """
    attempt = 0
    while True:
        resp: httpx.Response | None = None
        try:
            resp = await client.request(
                method, url, timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                **kwargs,
            )
            if resp.status_code < 400:
                return resp
            if resp.status_code not in RETRY_STATUS or attempt >= max_retries:
                raise UpstreamError(service, resp.status_code, resp.text)
            reason = f"HTTP {resp.status_code}"
        except httpx.TransportError as exc:  # includes timeouts
            if attempt >= max_retries:
                raise UpstreamError(service, None, f"{type(exc).__name__}: {exc}") from exc
            reason = type(exc).__name__

        delay = _retry_after(resp) or min(30.0, base_delay * 2**attempt)
        delay += random.uniform(0, 0.5)
        attempt += 1
        log.warning(
            "upstream retry",
            extra=ctx(service=service, attempt=attempt, reason=reason, delay=round(delay, 2)),
        )
        await asyncio.sleep(delay)
