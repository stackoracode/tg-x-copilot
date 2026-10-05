"""Minimal async client for the OpenAI-compatible CPA proxy (text, vision, Image2-compatible
image models). Jev is NOT OpenAI-compatible; see clients/jev.py.

Deliberately small instead of the `openai` SDK: it lets us share one httpx client, control
retries/timeouts precisely, and tolerate relay quirks (see `image_edit_mode`).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..logging_setup import ctx
from .http import UpstreamError, request_with_retry

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating code fences and leading prose."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


def image_data_url(data: bytes, mime: str | None) -> str:
    return f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(data).decode()}"


class OpenAICompatClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        name: str,
        base_url: str,
        api_key: str,
        timeout: float,
        max_retries: int,
    ) -> None:
        self.http = http
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def _post_json(self, path: str, payload: dict[str, Any], timeout: float | None = None
                         ) -> dict[str, Any]:
        resp = await request_with_retry(
            self.http, "POST", f"{self.base_url}{path}", service=self.name,
            max_retries=self.max_retries, timeout=timeout or self.timeout,
            headers=self._headers(), json=payload,
        )
        return resp.json()

    # ------------------------------------------------------------------ models

    async def list_models(self) -> list[dict[str, Any]]:
        resp = await request_with_retry(
            self.http, "GET", f"{self.base_url}/models", service=self.name,
            max_retries=1, timeout=15.0, headers=self._headers(),
        )
        data = resp.json()
        return list(data.get("data", data if isinstance(data, list) else []))

    # ------------------------------------------------------------------ chat

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = await self._post_json("/chat/completions", payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise UpstreamError(self.name, 200, f"unexpected chat response: {data!r}") from exc
        if isinstance(content, list):  # some relays return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage = data.get("usage") or {}
        log.debug("chat ok", extra=ctx(service=self.name, model=model, usage=usage))
        return content or ""

    async def chat_json(
        self,
        model: str,
        messages: list[dict[str, Any]],
        schema: type[M],
        *,
        temperature: float | None = None,
        json_mode: bool = True,
        repair_attempts: int = 1,
    ) -> M:
        """Chat and validate the reply against a Pydantic schema, asking the model to repair
        invalid JSON up to `repair_attempts` times."""
        convo = list(messages)
        for attempt in range(repair_attempts + 1):
            text = await self.chat(model, convo, temperature=temperature, json_mode=json_mode)
            try:
                return schema.model_validate(extract_json(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                if attempt >= repair_attempts:
                    raise UpstreamError(self.name, 200, f"invalid JSON from model: {exc}") from exc
                log.warning("model returned invalid JSON; repairing",
                            extra=ctx(service=self.name, model=model))
                convo += [
                    {"role": "assistant", "content": text[:4000]},
                    {"role": "user", "content": (
                        f"Your reply was not valid for the required JSON schema: {exc}. "
                        "Reply again with ONLY the corrected JSON object.")},
                ]
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ images

    async def _image_bytes(self, data: dict[str, Any]) -> bytes:
        try:
            item = data["data"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise UpstreamError(self.name, 200, f"unexpected image response: {str(data)[:300]}"
                                ) from exc
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            resp = await request_with_retry(
                self.http, "GET", item["url"], service=f"{self.name}-image-download",
                max_retries=self.max_retries, timeout=60.0,
            )
            return resp.content
        raise UpstreamError(self.name, 200, "image response has neither b64_json nor url")

    async def images_generate(self, model: str, prompt: str, *, size: str,
                              references: list[tuple[bytes, str | None]] | None = None) -> bytes:
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "size": size, "n": 1}
        if references:
            payload["image_urls"] = [image_data_url(b, m) for b, m in references]
        data = await self._post_json("/images/generations", payload, timeout=max(self.timeout, 180))
        return await self._image_bytes(data)

    async def images_edit(self, model: str, prompt: str, image: bytes, mime: str | None, *,
                          size: str, mode: str = "edits") -> bytes:
        if mode == "generations_with_refs":
            return await self.images_generate(model, prompt, size=size,
                                              references=[(image, mime)])
        ext = (mime or "image/png").split("/")[-1].replace("jpeg", "jpg")
        resp = await request_with_retry(
            self.http, "POST", f"{self.base_url}/images/edits", service=self.name,
            max_retries=self.max_retries, timeout=max(self.timeout, 180),
            headers=self._headers(),
            data={"model": model, "prompt": prompt, "size": size, "n": "1"},
            files={"image": (f"source.{ext}", image, mime or "image/png")},
        )
        return await self._image_bytes(resp.json())
