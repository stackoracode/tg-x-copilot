"""FastAPI app factory. The lifespan owns the whole AppContext (bot + workers live in the same
event loop as the web server), so uvicorn's SIGTERM handling gives us graceful shutdown."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..app_context import AppContext
from ..config import AppSettings
from .admin import router as admin_router
from .compose import router as compose_router


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx = AppContext(settings)
        app.state.ctx = ctx
        try:
            await ctx.start()
            yield
        finally:
            await ctx.stop()

    app = FastAPI(title="tg-x-copilot", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, object]:
        ctx: AppContext = app.state.ctx
        telegram = (bool(ctx.telegram and ctx.telegram.connected)
                    if ctx.config.base.telegram.enabled else "disabled")
        return {"ok": True, "workers": ctx.workers.stats, "telegram": telegram}

    # Compose routes first: admin's catch-all POST /tasks/{id}/{action} would shadow
    # POST /tasks/{id}/images otherwise.
    app.include_router(compose_router)
    app.include_router(admin_router)
    return app
