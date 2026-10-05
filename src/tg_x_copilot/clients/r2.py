"""Async Cloudflare R2 client (S3 API, AWS SigV4) on top of the shared httpx client.

Implements only what the MVP needs: put/get/head/delete object, head bucket, presigned GET.
Objects are written with `x-amz-storage-class: STANDARD` (configurable) so they stay in R2
Standard storage, which is what the free tier covers.
Avoids boto3 (sync) so R2 transfers never block the event loop.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from ..config import R2Settings
from .http import UpstreamError, request_with_retry

_UNSIGNED = "UNSIGNED-PAYLOAD"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _uri_encode(value: str, safe: str = "-_.~") -> str:
    return quote(value, safe=safe)


class R2Client:
    def __init__(self, http: httpx.AsyncClient, cfg: R2Settings) -> None:
        self.http = http
        self.cfg = cfg
        self.endpoint = cfg.endpoint_url
        self.host = httpx.URL(self.endpoint).host
        self._ak = cfg.access_key_id.get_secret_value()
        self._sk = cfg.secret_access_key.get_secret_value()

    @property
    def configured(self) -> bool:
        return bool(self._ak and self._sk and (self.cfg.account_id or self.cfg.endpoint))

    # ------------------------------------------------------------------ signing

    def _path(self, key: str = "") -> str:
        path = f"/{self.cfg.bucket}"
        if key:
            path += "/" + _uri_encode(key, safe="-_.~/")
        return path

    def _signing_key(self, date: str) -> bytes:
        k = _hmac(("AWS4" + self._sk).encode(), date)
        k = _hmac(k, self.cfg.region)
        k = _hmac(k, "s3")
        return _hmac(k, "aws4_request")

    def _sign_headers(self, method: str, path: str, payload_hash: str,
                      amz_headers: dict[str, str] | None = None) -> dict[str, str]:
        """amz_headers: extra x-amz-* headers; SigV4 requires them to be signed."""
        now = datetime.now(timezone.utc)
        amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        headers = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date,
                   **{k.lower(): v.strip() for k, v in (amz_headers or {}).items()}}
        signed = ";".join(sorted(headers))
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        canonical_request = "\n".join(
            [method, path, "", canonical_headers, signed, payload_hash]
        )
        scope = f"{date}/{self.cfg.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        signature = hmac.new(self._signing_key(date), string_to_sign.encode(),
                             hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self._ak}/{scope}, SignedHeaders={signed}, "
            f"Signature={signature}"
        )
        del headers["host"]  # httpx sets it
        return headers

    def presign_get(self, key: str, ttl: int | None = None) -> str:
        ttl = ttl or self.cfg.presign_ttl_seconds
        now = datetime.now(timezone.utc)
        amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        scope = f"{date}/{self.cfg.region}/s3/aws4_request"
        path = self._path(key)
        params = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self._ak}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(ttl),
            "X-Amz-SignedHeaders": "host",
        }
        query = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(params.items()))
        canonical_request = "\n".join(
            ["GET", path, query, f"host:{self.host}\n", "host", _UNSIGNED]
        )
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        signature = hmac.new(self._signing_key(date), string_to_sign.encode(),
                             hashlib.sha256).hexdigest()
        return f"{self.endpoint}{path}?{query}&X-Amz-Signature={signature}"

    def public_url(self, key: str) -> str | None:
        if not self.cfg.public_base_url:
            return None
        return f"{self.cfg.public_base_url.rstrip('/')}/{_uri_encode(key, safe='-_.~/')}"

    # ------------------------------------------------------------------ operations

    async def _request(self, method: str, key: str = "", *, content: bytes = b"",
                       extra_headers: dict[str, str] | None = None,
                       amz_headers: dict[str, str] | None = None,
                       ok_404: bool = False) -> httpx.Response | None:
        if not self.configured:
            raise UpstreamError("r2", None, "R2 is not configured")
        path = self._path(key)
        payload_hash = hashlib.sha256(content).hexdigest()
        # Signed once: retries resend within the max backoff (~1 min), well inside the
        # 15-minute SigV4 clock-skew window.
        headers = self._sign_headers(method, path, payload_hash, amz_headers)
        if extra_headers:
            headers.update(extra_headers)
        try:
            return await request_with_retry(
                self.http, method, f"{self.endpoint}{path}", service="r2",
                max_retries=self.cfg.max_retries, timeout=self.cfg.timeout_seconds,
                headers=headers, content=content or None,
            )
        except UpstreamError as exc:
            if ok_404 and exc.status == 404:
                return None
            raise

    async def put_object(self, key: str, data: bytes, content_type: str) -> None:
        await self._request(
            "PUT", key, content=data,
            extra_headers={"content-type": content_type,
                           "cache-control": "private, max-age=31536000, immutable"},
            amz_headers={"x-amz-storage-class": self.cfg.storage_class},
        )

    async def delete_object(self, key: str) -> None:
        await self._request("DELETE", key, ok_404=True)

    async def get_object(self, key: str) -> bytes:
        resp = await self._request("GET", key)
        assert resp is not None
        return resp.content

    async def exists(self, key: str) -> bool:
        return await self._request("HEAD", key, ok_404=True) is not None

    async def head_bucket(self) -> None:
        await self._request("HEAD")
