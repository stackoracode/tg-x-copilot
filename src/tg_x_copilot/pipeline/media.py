"""CPU-bound image helpers (always called via asyncio.to_thread). Pure, in-memory, no temp files.

Compression policy (R2 free tier friendly, X-compatible formats only: JPEG/PNG):
- Graphics (screenshots, charts, infographics, text-heavy or flat-color images) stay LOSSLESS PNG
  so text is never smeared; only downscaled if the long side exceeds `graphic_max_side`.
- Photos become progressive JPEG at `jpeg_quality`, long side <= `photo_max_side`.
- An already-compressed JPEG photo without metadata and within limits is kept byte-for-byte
  (re-encoding would only add generational loss). Telegram "photo" uploads usually hit this path.
- EXIF orientation is applied; all metadata (EXIF/GPS/text chunks) is dropped.
- If re-encoding would not shrink a same-format, metadata-free original, the original wins.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

_FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}
_MIME_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
GRAPHIC_TYPES = {"chart", "infographic", "screenshot", "illustration"}
PHOTO_TYPES = {"photo_real_event", "photo_generic"}


@dataclass(frozen=True)
class ImageBlob:
    data: bytes
    mime: str
    width: int
    height: int
    sha256: str

    @property
    def ext(self) -> str:
        return _MIME_EXT.get(self.mime, "bin")

    @property
    def size(self) -> int:
        return len(self.data)


def _blob(data: bytes, mime: str, size: tuple[int, int]) -> ImageBlob:
    return ImageBlob(data, mime, size[0], size[1], hashlib.sha256(data).hexdigest())


def inspect_image(data: bytes) -> ImageBlob | None:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            return _blob(data, _FORMAT_MIME.get(img.format or "", "application/octet-stream"),
                         img.size)
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return None


def graphic_hint(image_type: str | None) -> bool | None:
    if image_type in GRAPHIC_TYPES:
        return True
    if image_type in PHOTO_TYPES:
        return False
    return None


def looks_graphic(img: Image.Image) -> bool:
    """Flat-color content (few distinct colors on a thumbnail) => screenshot/chart/text."""
    thumb = img.convert("RGB")
    thumb.thumbnail((160, 160))
    return thumb.getcolors(maxcolors=3000) is not None


def optimize_image(data: bytes, *, graphic: bool | None, photo_max_side: int,
                   graphic_max_side: int, jpeg_quality: int) -> ImageBlob:
    with Image.open(io.BytesIO(data)) as src:
        src_format = src.format or ""
        src_size = src.size
        has_meta = (len(src.getexif()) > 0
                    or any(k in src.info for k in ("exif", "xmp", "XML:com.adobe.xmp", "comment")))
        has_alpha = src.mode in ("RGBA", "LA") or (src.mode == "P" and "transparency" in src.info)
        img = ImageOps.exif_transpose(src) or src
        is_graphic = looks_graphic(img) if graphic is None else graphic
        max_side = graphic_max_side if is_graphic else photo_max_side
        within = max(src_size) <= max_side

        # Fast path: compressed JPEG photo, no metadata, within limits -> keep as-is.
        if not is_graphic and src_format == "JPEG" and not has_meta and within:
            return _blob(data, "image/jpeg", src_size)

        if not within:
            img = img.copy()
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

        out = io.BytesIO()
        if is_graphic or has_alpha:
            if img.mode not in ("1", "L", "LA", "P", "RGB", "RGBA"):
                img = img.convert("RGBA" if has_alpha else "RGB")
            img.save(out, format="PNG", optimize=True)
            mime = "image/png"
        else:
            img.convert("RGB").save(out, format="JPEG", quality=jpeg_quality, optimize=True,
                                    progressive=True)
            mime = "image/jpeg"
        new = out.getvalue()
        new_size = img.size

    same_format = _FORMAT_MIME.get(src_format) == mime
    if same_format and within and not has_meta and len(new) >= len(data):
        return _blob(data, mime, src_size)
    return _blob(new, mime, new_size)
