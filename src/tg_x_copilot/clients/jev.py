"""Async client for TypeSafe AI's System One API (Jev).

Jev is not a chat model: it evaluates a text `state` against named, typed questions and returns
typed answers with probabilities (and `confidence` for choice/score). Reference:
https://docs.typesafe.ai/api, https://docs.typesafe.ai/confidence

    POST {base}/systemone   Authorization: Bearer <key>
    {"model": "jev-latest", "state": <str|object|array>, "questions": {"id": {...}}}

Question shapes:
    noul   {"type":"noul",   "instructions": str, "criteria": {"true": str, "false": str}?}
    choice {"type":"choice", "instructions": str, "criteria": {option: description, ...}}
    score  {"type":"score",  "instructions": str, "criteria": [level0, level1, ...]}  (2-10)
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from ..logging_setup import ctx
from .http import UpstreamError, request_with_retry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------- question builders


def noul(instructions: str, true: str | None = None, false: str | None = None
         ) -> dict[str, Any]:
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {"true": true or "", "false": false or ""}
    return q


def choice(instructions: str, options: dict[str, str | None]) -> dict[str, Any]:
    if not 2 <= len(options) <= 255:
        raise ValueError("choice needs 2..255 options")
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, levels: list[str]) -> dict[str, Any]:
    if not 2 <= len(levels) <= 10:
        raise ValueError("score needs 2..10 levels")
    return {"type": "score", "instructions": instructions, "criteria": levels}


# ---------------------------------------------------------------------- responses


class JevAnswer(BaseModel):
    type: Literal["noul", "choice", "score"]
    noul: float | None = None
    choice: str | None = None
    score: float | None = None
    probabilities: dict[str, float] = Field(default_factory=dict)
    legend: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None  # absent for noul: the probability itself is the signal

    def normalized_score(self) -> float:
        """Score mapped to 0..1 (score ranges 0..n-1 over n levels)."""
        levels = max(len(self.legend) or len(self.probabilities), 2)
        return max(0.0, min(1.0, (self.score or 0.0) / (levels - 1)))


class JevResponse(BaseModel):
    model: str = ""
    answers: dict[str, JevAnswer]
    usage: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------- client


class JevClient:
    def __init__(self, http: httpx.AsyncClient, *, base_url: str, api_key: str, model: str,
                 timeout: float, max_retries: int) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self.base_url)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _require_key(self) -> None:
        if not self.configured:
            raise UpstreamError("jev", None, "JEV__API_KEY is not configured")

    async def list_models(self) -> list[dict[str, Any]]:
        """GET /v1/models -> [{name, description, release_date}, ...]"""
        self._require_key()
        resp = await request_with_retry(
            self.http, "GET", f"{self.base_url}/models", service="jev", max_retries=1,
            timeout=15.0, headers=self._headers(),
        )
        data = resp.json()
        items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        return [m for m in items if isinstance(m, dict)]

    async def ask(self, state: str | dict[str, Any] | list[Any],
                  questions: dict[str, dict[str, Any]]) -> JevResponse:
        self._require_key()
        resp = await request_with_retry(
            self.http, "POST", f"{self.base_url}/systemone", service="jev",
            max_retries=self.max_retries, timeout=self.timeout, headers=self._headers(),
            json={"model": self.model, "state": state, "questions": questions},
        )
        try:
            parsed = JevResponse.model_validate(resp.json())
        except ValueError as exc:
            raise UpstreamError("jev", resp.status_code, f"unexpected response: {exc}") from exc
        missing = set(questions) - set(parsed.answers)
        if missing:
            raise UpstreamError("jev", resp.status_code, f"answers missing for {sorted(missing)}")
        log.debug("jev ok", extra=ctx(model=parsed.model, usage=parsed.usage))
        return parsed
