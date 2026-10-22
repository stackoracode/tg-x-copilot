"""Automatic, in-memory information-card fallback when the image provider is unavailable."""

from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..image_settings import InformationDensity
from ..models import ImageAnalysis, InputEnvelope, VerifiedFact, VerifiedFacts
from .image_content import editorial_evidence


def source_packet(env: InputEnvelope, analyses: dict[int, ImageAnalysis]) -> VerifiedFacts:
    """Use exact source excerpts, without a model rewriting numbers or an approval gate."""
    sources = [(None, env.text)] + [
        (idx, editorial_evidence(a)) for idx, a in sorted(analyses.items()) if not a.sensitive]
    sources = [(idx, text.strip()) for idx, text in sources if text.strip()]
    limit = max(1, 16000 // max(1, len(sources)))
    return VerifiedFacts(facts=[VerifiedFact(text=text[:limit], evidence=text[:limit], source_idx=idx)
                                for idx, text in sources])


def _font(size: int):
    for path in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _wrap(text: str, font, width: int) -> list[str]:
    lines = []
    for paragraph in text.splitlines():
        line = ""
        for char in paragraph:
            if line and font.getlength(line + char) > width:
                lines.append(line)
                line = ""
            line += char
        if line:
            lines.append(line)
    return lines


def render_card(post: str, packet: VerifiedFacts, *, density: InformationDensity,
                size: str, locale: str) -> bytes:
    """Render source text and the finished post; never ask another model to invent a visual."""
    width, height = {"1024x1536": (1024, 1536), "1536x1024": (1536, 1024)}.get(size, (1024, 1024))
    image = Image.new("RGB", (width, height), "#101c30")
    draw = ImageDraw.Draw(image)
    margin = 64
    body_font, title_font = _font(30), _font(40)
    draw.rounded_rectangle((margin, 48, margin + 72, 56), radius=4, fill="#49ddbc")
    label = "信息摘要" if locale == "zh-CN" else "KEY POINTS"
    draw.text((margin, 78), label, font=_font(24), fill="#49ddbc")
    y = 132
    # Keep the finished standard post as the card's main message, including identifiers.
    for line in _wrap(post, title_font, width - 2 * margin):
        if y + 55 > height - 90:
            break
        draw.text((margin, y), line, font=title_font, fill="#ffffff")
        y += 55
    y += 28
    max_points = {InformationDensity.LOW: 0, InformationDensity.MEDIUM: 3,
                  InformationDensity.HIGH: 6}[density]
    points = []
    for fact in packet.facts:
        for sentence in re.split(r"(?<=[。！？.!?])\s*|\n+", fact.text):
            sentence = sentence.strip()
            if (12 <= len(sentence) <= 180 and not re.search(r"https?://", sentence)
                    and sentence not in points and sentence not in post):
                points.append(sentence)
    for point in points[:max_points]:
        lines = _wrap(point, body_font, width - 2 * margin - 28)
        block_height = len(lines) * 42 + 24
        if y + block_height > height - 60:
            break
        draw.ellipse((margin, y + 14, margin + 8, y + 22), fill="#49ddbc")
        for line in lines:
            draw.text((margin + 28, y), line, font=body_font, fill="#dae3f2")
            y += 42
        y += 24
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
