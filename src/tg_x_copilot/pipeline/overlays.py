"""Permission-scoped promotion edits, never copyright removal.

Image2 supplies local patches. Only selected, validated rectangles are composited into an
EXIF-corrected source canvas; lossless output keeps every outside pixel and source dimension.
"""

from __future__ import annotations

import hashlib
import io
import math

from PIL import Image, ImageOps

from ..image_settings import ImageAction, ImageOption, ImageOptions
from ..models import ImageAnalysis, MarkRegion
from .media import ImageBlob


def wants_region_edit(action: ImageAction | None, options: ImageOptions) -> bool:
    return ImageOption.REMOVE_OVERLAYS in options.flags and (
        ImageOption.MINIMAL_CHANGES in options.flags or action == ImageAction.ENHANCE
    )


def intersects(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def approved_regions(analysis: ImageAnalysis, options: ImageOptions, idx: int) -> list[MarkRegion]:
    if analysis.has_source_copyright_mark is True and not any(
        region.kind != "promotion" for region in analysis.mark_regions
    ):
        raise ValueError("protected marks are not located")
    if analysis.has_third_party_watermark and analysis.has_source_copyright_mark is None:
        raise ValueError("legacy watermark classification is ambiguous")
    if len({region.id for region in analysis.mark_regions}) != len(analysis.mark_regions):
        raise ValueError("ambiguous duplicate mark identifiers")
    protected = [region for region in analysis.mark_regions if region.kind != "promotion"]
    targets = options.promotion_targets
    candidates = [
        region
        for region in analysis.mark_regions
        if region.kind == "promotion" and (not targets or f"{idx}.{region.id}" in targets)
    ]
    if not candidates:
        raise ValueError("no explicitly selected promotion region")
    if targets and any(
        key.startswith(f"{idx}.")
        and key.split(".", 1)[1] not in {region.id for region in candidates}
        for key in targets
    ):
        raise ValueError("selected region is not promotion")
    for region in candidates:
        if not region.safe_to_remove or region.confidence < 0.9:
            raise ValueError("promotion region is uncertain or overlaps meaningful content")
        if any(intersects(region.box, mark.box) for mark in protected):
            raise ValueError("promotion region overlaps protected attribution")
    return candidates


def source_canvas(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as source:
        return ImageOps.exif_transpose(source).convert("RGBA")


def pixels(box: tuple, size: tuple[int, int]) -> tuple[int, int, int, int]:
    w, h = size
    return (
        math.floor(box[0] * w),
        math.floor(box[1] * h),
        math.ceil(box[2] * w),
        math.ceil(box[3] * h),
    )


def crop_region(data: bytes, region: MarkRegion) -> bytes:
    source = source_canvas(data)
    output = io.BytesIO()
    source.crop(pixels(region.box, source.size)).save(output, format="PNG")
    return output.getvalue()


def composite_patches(
    source_data: bytes, regions: list[MarkRegion], patches: list[bytes]
) -> ImageBlob:
    if len(regions) != len(patches) or not regions:
        raise ValueError("missing promotion patch")
    canvas = source_canvas(source_data)
    for region, patch in zip(regions, patches):
        rectangle = pixels(region.box, canvas.size)
        with Image.open(io.BytesIO(patch)) as candidate:
            edited = candidate.convert("RGBA").resize(
                (rectangle[2] - rectangle[0], rectangle[3] - rectangle[1]), Image.Resampling.LANCZOS
            )
        # Local patch resizing never changes original subject, canvas, UI or aspect ratio.
        canvas.paste(edited, (rectangle[0], rectangle[1]))
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    data = output.getvalue()
    return ImageBlob(
        data, "image/png", canvas.width, canvas.height, hashlib.sha256(data).hexdigest()
    )


def unchanged_outside(source_data: bytes, candidate_data: bytes, regions: list[MarkRegion]) -> bool:
    source, candidate = source_canvas(source_data), source_canvas(candidate_data)
    if source.size != candidate.size:
        return False
    from PIL import ImageChops, ImageDraw

    diff = ImageChops.difference(source, candidate)
    painter = ImageDraw.Draw(diff)
    for region in regions:
        left, top, right, bottom = pixels(region.box, source.size)
        painter.rectangle((left, top, right - 1, bottom - 1), fill=(0, 0, 0, 0))
    return all(channel.getbbox() is None for channel in diff.split())


def validate_pixel_scope(data: bytes, regions: list[MarkRegion], analysis: ImageAnalysis) -> None:
    size = source_canvas(data).size
    protected = [
        pixels(mark.box, size) for mark in analysis.mark_regions if mark.kind != "promotion"
    ]
    for region in regions:
        rect = pixels(region.box, size)
        if (rect[2] - rect[0]) * (rect[3] - rect[1]) > size[0] * size[1] * 0.25:
            raise ValueError("promotion region too large for a minimal edit")
        if any(intersects(rect, mark) for mark in protected):
            raise ValueError("rounded promotion region overlaps a protected mark")
