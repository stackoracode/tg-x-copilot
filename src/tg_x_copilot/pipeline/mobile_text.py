"""One phone-sized, text-dominant image: select important facts, then typeset real text."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from .quick_card import _font

WIDTH, HEIGHT = 1440, 2560
SOURCE_LIMIT = 120000


class CardSection(BaseModel):
    title: str
    points: list[str] = Field(default_factory=list)


class MobileCardPlan(BaseModel):
    title: str
    intro: str = ""
    sections: list[CardSection] = Field(default_factory=list)


@dataclass(frozen=True)
class RenderedCard:
    data: bytes
    text: str
    omitted_points: int


def fallback_plan(post: str, source: str, locale: str) -> MobileCardPlan:
    """If outlining is unavailable, keep the standard post and source-derived key sentences."""
    candidates = []
    seen = set()
    tokens = set(re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,4}", post))
    for index, part in enumerate(re.split(r"(?<=[。！？])|(?<=[.!?])\s+|\n+", source)):
        part = part.strip()
        if not 12 <= len(part) <= 160 or part in seen or "https://" in part or "http://" in part:
            continue
        seen.add(part)
        score = sum(token in part for token in tokens) + 2 * bool(re.search(r"\d", part))
        candidates.append((-score, index, part))
    points = [part for _, _, part in sorted(candidates)[:12]]
    return MobileCardPlan(title="重点信息" if locale == "zh-CN" else "KEY INFORMATION",
        intro=post, sections=[CardSection(title="核心要点" if locale == "zh-CN" else "KEY POINTS",
                                         points=points)])


def render_mobile_card(plan: MobileCardPlan, locale: str) -> RenderedCard:
    """Fixed 9:16 canvas and legible fonts. Drop whole lower-priority points, never shrink forever."""
    image = Image.new("RGB", (WIDTH, HEIGHT), "#f5f7fb")
    draw = ImageDraw.Draw(image)
    margin, available = 88, WIDTH - 176
    title_font, heading_font, body_font = _font(76), _font(52), _font(50)
    small_font = _font(32)
    widths = {}

    def wrap(text, font, width):
        lines = []
        for paragraph in text.splitlines():
            line, length = "", 0.0
            for char in paragraph:
                key = (id(font), char)
                if key not in widths:
                    widths[key] = font.getlength(char)
                delta = widths[key]
                # Hang one closing punctuation mark into the generous right margin.
                # This avoids a separate line containing only a Chinese full stop.
                closing = char in "，。！？；：、）】》〉」』,.!?;:)]}"
                if line and length + delta > width and not (closing and length + delta <= width + font.size):
                    lines.append(line)
                    line, length = "", 0.0
                line += char
                length += delta
            if line:
                lines.append(line)
        return lines

    label = "重点信息 · 手机阅读" if locale == "zh-CN" else "ESSENTIALS · MOBILE BRIEF"
    draw.rounded_rectangle((margin, 70, margin + 12, 110), radius=5, fill="#2760db")
    draw.text((margin + 30, 70), label, font=small_font, fill="#52627b")
    y, printed = 150, []
    title = " ".join(plan.title.split())
    title_lines = wrap(title, title_font, available)
    # Very long model titles fall back to a neutral heading rather than consuming the card.
    if not title_lines or len(title_lines) > 3:
        title_lines = ["重点信息" if locale == "zh-CN" else "KEY INFORMATION"]
    for line in title_lines:
        draw.text((margin, y), line, font=title_font, fill="#14233d")
        y += 99
    printed += title_lines
    y += 26
    intro_lines = wrap(plan.intro.strip(), body_font, available - 56)
    if intro_lines and len(intro_lines) <= 5:
        box_height = len(intro_lines) * 70 + 46
        draw.rounded_rectangle((margin, y, WIDTH - margin, y + box_height), radius=20, fill="#e7eefc")
        y += 23
        for line in intro_lines:
            draw.text((margin + 28, y), line, font=body_font, fill="#18376c")
            y += 70
        y += 48
        printed += intro_lines

    omitted, bottom = 0, HEIGHT - 130
    for number, section in enumerate(plan.sections, 1):
        heading_lines = wrap(section.title.strip(), heading_font, available - 78)
        if len(heading_lines) > 2:
            heading_lines = ["要点" if locale == "zh-CN" else "DETAILS"]
        rows = [(point, wrap(point.strip(), body_font, available - 40)) for point in section.points if point.strip()]
        # Render a heading only when at least one complete point fits below it.
        minimum = min((len(lines) * 69 + 22 for _, lines in rows), default=0)
        heading_height = len(heading_lines) * 74 + 22
        if not rows or y + heading_height + minimum > bottom:
            omitted += len(rows)
            continue
        draw.rounded_rectangle((margin, y + 8, margin + 52, y + 60), radius=12, fill="#2760db")
        draw.text((margin + 11, y + 10), str(number), font=small_font, fill="white")
        for line in heading_lines:
            draw.text((margin + 78, y), line, font=heading_font, fill="#14233d")
            y += 74
        y += 22
        printed += heading_lines
        for _, lines in rows:
            height = len(lines) * 69 + 22
            if y + height > bottom:
                omitted += 1
                continue
            draw.ellipse((margin + 3, y + 26, margin + 13, y + 36), fill="#2760db")
            for line in lines:
                draw.text((margin + 40, y), line, font=body_font, fill="#26364d")
                y += 69
            printed += lines
            y += 22
        y += 28
    footer = "长文提炼 · 保留重点" if locale == "zh-CN" else "LONG READ · KEY FACTS"
    draw.line((margin, HEIGHT - 92, WIDTH - margin, HEIGHT - 92), fill="#cdd7e6", width=2)
    draw.text((margin, HEIGHT - 68), footer, font=_font(26), fill="#687990")
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return RenderedCard(out.getvalue(), "\n".join(printed), omitted)
