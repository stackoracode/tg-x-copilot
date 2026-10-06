"""Shared fakes for reliability tests. No network, no MySQL, no Telegram."""

from __future__ import annotations

import inspect
import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from tg_x_copilot.config import AppSettings
from tg_x_copilot.i18n import I18n
from tg_x_copilot.pipeline.limits import Limits


class Recorder:
    """Async fake: any awaited method call is recorded; return values come from `returns`."""

    def __init__(self, **returns: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.returns = {"previous_uses": [], "list_media": [],
                        "send_file": lambda chat, files, **kw: (
                            [SimpleNamespace(photo=object(), id=idx+1) for idx, _ in enumerate(files)]
                            if isinstance(files, list) else SimpleNamespace(photo=object(), id=1)),
                        **returns}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        async def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            value = self.returns.get(name)
            if isinstance(value, BaseException):
                raise value
            if callable(value):
                value = value(*args, **kwargs)
                if inspect.isawaitable(value):
                    value = await value
            return value

        return method

    def named(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(a, k) for n, a, k in self.calls if n == name]


def png_bytes(color: str = "white", size: tuple[int, int] = (64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(_env_file=None)


@pytest.fixture
def app(settings: AppSettings) -> SimpleNamespace:
    jev = Recorder()
    jev.configured = True  # type: ignore[attr-defined]
    return SimpleNamespace(
        config=SimpleNamespace(current=settings, base=settings),
        repo=Recorder(),
        limits=Limits.from_settings(settings.concurrency),
        i18n=I18n(),
        hub=SimpleNamespace(jev=jev, cpa=Recorder(), r2=Recorder()),
        storage=Recorder(),
        telegram=None,
        instance_id="test-host:1:abc",
        shutting_down=False,
    )


def sent_files(file_arg):
    return file_arg if isinstance(file_arg, list) else [file_arg]
