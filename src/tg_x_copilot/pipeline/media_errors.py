"""Localized failure categories; raw upstream details may contain credentials or source data."""

import httpx
from PIL import UnidentifiedImageError

from ..i18n import t


def media_error_reason(exc: Exception, locale: str) -> str:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return t(locale, "error_http", status=status)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return t(locale, "error_timeout")
    if isinstance(exc, (ConnectionError, httpx.TransportError)):
        return t(locale, "error_connection")
    if isinstance(exc, (ValueError, UnidentifiedImageError)):
        return t(locale, "error_invalid_image")
    return t(locale, "error_rejected")
