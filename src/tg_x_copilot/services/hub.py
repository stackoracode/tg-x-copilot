"""Reusable upstream clients built from the current settings.

All clients share one httpx.AsyncClient (connection pooling). `configure()` is cheap and is called
again whenever runtime settings change, so new keys/URLs apply without a restart.
"""

from __future__ import annotations

from ..clients import JevClient, OpenAICompatClient, R2Client, make_http_client
from ..config import AppSettings


class ClientHub:
    def __init__(self) -> None:
        self.http = make_http_client()
        self.cpa: OpenAICompatClient
        self.jev: JevClient
        self.r2: R2Client

    def configure(self, s: AppSettings) -> None:
        self.cpa = OpenAICompatClient(
            self.http, name="cpa", base_url=s.cpa.base_url,
            api_key=s.cpa.api_key.get_secret_value(),
            timeout=s.cpa.timeout_seconds, max_retries=s.cpa.max_retries,
        )
        self.jev = JevClient(
            self.http, base_url=s.jev.base_url, api_key=s.jev.api_key.get_secret_value(),
            model=s.jev.model, timeout=s.jev.timeout_seconds, max_retries=s.jev.max_retries,
        )
        self.r2 = R2Client(self.http, s.r2)

    async def close(self) -> None:
        await self.http.aclose()
