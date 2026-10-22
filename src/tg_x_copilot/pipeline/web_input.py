"""Turn a web compose submission into one InputEnvelope (the web counterpart of normalizer.py).

One submission = one task. Typed/pasted text is message 1; each fetched page is its own
message (so the per-message cleaning step treats it like a separate forwarded post); uploaded
images are the "album". Media message ids are 1..n so the processor can map loaded bytes back.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import WEB_CHAT_ID, WEB_USER_ID, InputEnvelope, MediaKind, SourceMedia
from .media import ImageBlob, inspect_image

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@dataclass(frozen=True)
class WebPage:
    url: str
    text: str


def validate_uploads(files: list[tuple[str, bytes]], *, max_files: int, max_bytes: int
                     ) -> tuple[list[ImageBlob], list[str]]:
    """Return (valid images in order, problems). Content is sniffed with Pillow; the browser's
    filename / content type are never trusted. CPU-bound: call via asyncio.to_thread."""
    images: list[ImageBlob] = []
    problems: list[str] = []
    for name, data in files:
        if not data:
            continue
        if len(images) >= max_files:
            problems.append(f"{name}: more than {max_files} images; extra files ignored")
            continue
        if len(data) > max_bytes:
            problems.append(f"{name}: larger than {max_bytes // (1024 * 1024)} MB")
            continue
        blob = inspect_image(data)
        if blob is None or blob.mime not in _ALLOWED_MIME:
            problems.append(f"{name}: not a supported image (JPEG, PNG, WebP, GIF)")
            continue
        images.append(blob)
    return images, problems


def split_urls(raw: str) -> list[str]:
    seen: list[str] = []
    for token in raw.replace(",", " ").split():
        token = token.strip()
        if token.startswith(("http://", "https://")) and token not in seen:
            seen.append(token)
    return seen


def build_web_envelope(*, text: str, urls: list[str], pages: list[WebPage],
                       images: list[ImageBlob], own_content: bool, locale: str,
                       market: str) -> InputEnvelope:
    from ..services.url_fetch import URL_RE

    text = text.strip()
    inline = [u for u in URL_RE.findall(text)]
    first_urls = list(dict.fromkeys(inline + urls))
    message_texts: list[str] = []
    message_urls: list[list[str]] = []
    if text or first_urls:
        # A URL-only submission still needs a message so the links reach the editor.
        message_texts.append(text or "\n".join(first_urls))
        message_urls.append(first_urls)
    for page in pages:
        if page.text.strip():
            message_texts.append(page.text.strip())
            message_urls.append([page.url])
    media = [
        SourceMedia(message_id=i + 1, kind=MediaKind.IMAGE_DOCUMENT, mime=blob.mime,
                    size=blob.size,
                    # Rights are never inferred: "from elsewhere" is treated like a forward of
                    # unknown origin; "own content" like a direct upload.
                    forwarded=not own_content, source_chat_id=None)
        for i, blob in enumerate(images)
    ]
    all_urls = list(dict.fromkeys(first_urls + [p.url for p in pages]))
    return InputEnvelope(
        channel="web",
        chat_id=WEB_CHAT_ID,
        user_id=WEB_USER_ID,
        message_ids=list(range(1, max(1, len(images)) + 1)),
        text="\n\n".join(message_texts),
        message_texts=message_texts,
        message_urls=message_urls,
        urls=all_urls,
        media=media,
        is_direct=own_content,
        locale=locale,
        market=market,
    )
