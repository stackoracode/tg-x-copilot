"""Verify generated narrative separately from facts, source OCR and code identifiers."""
from __future__ import annotations

import json

from pydantic import BaseModel

from .. import prompts
from ..i18n import Locale
from .guards import wrong_language


class LanguageCheck(BaseModel):
    passed: bool


async def is_localized(strings: list[str], locale: Locale, app) -> bool:
    values = [value for value in strings if value.strip()]
    if not values:
        return True
    if any(wrong_language(value, locale.code) for value in values):
        return False
    cfg = app.config.current
    p = prompts.render('language_check', locale.code, language_name=locale.language_name,
                       strings=json.dumps(values, ensure_ascii=False))
    async with app.limits.text:
        verdict = await app.hub.cpa.chat_json(cfg.models.text_model, p.messages(), LanguageCheck,
                                             json_mode=cfg.models.json_mode)
    return verdict.passed
