"""Entry point: `python -m tg_x_copilot` or `tg-x-copilot`."""

from __future__ import annotations

import uvicorn

from .config import AppSettings
from .logging_setup import setup_logging
from .web import create_app


def main() -> None:
    settings = AppSettings()
    setup_logging(settings.log_level, settings.log_json)
    uvicorn.run(
        create_app(settings),
        host=settings.admin.host,
        port=settings.admin.port,
        log_config=None,  # keep our structured, masked logging
        timeout_graceful_shutdown=int(settings.shutdown_grace_seconds) + 5,
    )


if __name__ == "__main__":
    main()
