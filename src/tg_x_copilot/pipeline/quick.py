"""Quick mode uses the standard pipeline; only input normalization lives here."""

from __future__ import annotations

import re

from ..models import InputEnvelope

_COMMAND = re.compile(r"^\s*/quick(?:@\w+)?\b\s*", re.IGNORECASE)


def strip_command(env: InputEnvelope) -> InputEnvelope:
    """Drop a leading `/quick` (Telegram one-shot command) from the source text."""
    texts = [_COMMAND.sub("", text, count=1) for text in env.message_texts]
    return env.model_copy(update={"text": _COMMAND.sub("", env.text, count=1).strip(),
                                  "message_texts": texts})
