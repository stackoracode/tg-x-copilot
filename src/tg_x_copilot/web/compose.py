"""Web compose channel: create and drive tasks from any browser, without Telegram.

Routes (all behind the same CSRF + Basic-auth dependencies as the admin UI):
    GET  /new                          compose form (mobile-first)
    POST /new                          text + URLs + images -> one task (standard or quick)
    POST /api/links/preview            try reading URLs now (which strategy works, excerpt)
    POST /tasks/{id}/images            re-run images only, with another action/options
    POST /tasks/{id}/authorize/{idx}   confirm editing rights for one blocked image
    GET  /api/tasks/{id}/status        lightweight status for live progress polling
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile

from ..logging_setup import ctx as log_ctx
from ..models import WEB_USER_ID
from ..pipeline.web_input import build_web_envelope, split_urls, validate_uploads
from .admin import _back, _ctx, _render, require_admin
from .forms import form_context, preferences_from_form
from .security import require_same_origin

log = logging.getLogger(__name__)


def require_web_enabled(request: Request) -> None:
    if not _ctx(request).config.base.web.enabled:
        raise HTTPException(404)


router = APIRouter(dependencies=[Depends(require_same_origin), Depends(require_admin),
                                 Depends(require_web_enabled)])

MAX_URLS = 5


def _tr(request: Request, key: str, **kw: Any) -> str:
    ctx = _ctx(request)
    return ctx.i18n.t(ctx.config.current.default_locale, key, **kw)


async def _read_uploads(form: Any, max_bytes: int) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for item in form.getlist("images"):
        if not isinstance(item, UploadFile) or not item.filename:
            continue
        data = await item.read(max_bytes + 1)  # one byte over the cap is enough to reject
        files.append((item.filename, data))
        await item.close()
    return files


@router.get("/new", response_class=HTMLResponse)
async def compose_page(request: Request) -> HTMLResponse:
    prefs = await _ctx(request).image_preferences.get(WEB_USER_ID)
    return _render(request, "new.html", **form_context(_ctx(request), prefs))


@router.post("/new")
async def compose_submit(request: Request) -> RedirectResponse:
    ctx = _ctx(request)
    cfg = ctx.config.current
    form = await request.form()
    text = str(form.get("text") or "")
    urls = split_urls(str(form.get("urls") or ""))[:MAX_URLS]
    own_content = bool(form.get("own_content"))
    try:
        prefs = preferences_from_form(form, authorized_media=bool(form.get("rights")))
    except ValueError:
        return _back("/new", _tr(request, "web_invalid_options"))

    uploads = await _read_uploads(form, cfg.pipeline.max_media_bytes)
    images, problems = await asyncio.to_thread(
        validate_uploads, uploads, max_files=cfg.web.max_files,
        max_bytes=cfg.pipeline.max_media_bytes)

    env = build_web_envelope(text=text, urls=urls, pages=[], images=images,
                             own_content=own_content, locale=cfg.default_locale,
                             market=cfg.market)
    # Linked pages are read by the worker (direct -> reader -> browser -> AI), so a slow or
    # blocked site never holds up this request; results show on the task page.
    env.fetch_links = bool(cfg.web.fetch_urls and form.get("fetch_pages"))
    env.fetch_ai = bool(form.get("fetch_ai"))
    env.quick_instructions = str(form.get("instructions") or "").strip()[:2000]
    if not env.text and not env.media:
        return _back("/new", "; ".join([_tr(request, "web_nothing_to_submit"), *problems]))
    # Remember the choices as the web operator's profile (regenerate re-applies it).
    await ctx.image_preferences.update(WEB_USER_ID, lambda _previous: prefs)
    task_id = await ctx.submit_web(env, prefs, images)
    if task_id is None:
        return _back("/new", _tr(request, "web_nothing_to_submit"))
    message = _tr(request, "web_task_created", task_id=task_id[:8])
    return _back(f"/tasks/{task_id}", "; ".join([message, *problems]))


@router.post("/api/links/preview")
async def preview_links(request: Request) -> JSONResponse:
    """The "Test links" button: read each URL now and report which strategy worked."""
    ctx = _ctx(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON body required") from None
    urls = split_urls(str(body.get("urls") or ""))[:MAX_URLS]
    if not urls:
        return JSONResponse({"results": []})
    allow_ai = bool(body.get("allow_ai"))
    results = await asyncio.gather(*(ctx.links.read(u, allow_ai=allow_ai) for u in urls),
                                   return_exceptions=True)
    out = []
    for url, result in zip(urls, results):
        if isinstance(result, BaseException):
            log.error("link preview failed", extra=log_ctx(url=url, error=type(result).__name__))
            out.append({"url": url, "ok": False, "attempts": [
                {"method": "error", "ok": False, "detail": type(result).__name__}]})
        else:
            out.append(result.as_json())
    return JSONResponse({"results": out})


@router.post("/tasks/{task_id}/images")
async def rerun_images(request: Request, task_id: str) -> RedirectResponse:
    ctx = _ctx(request)
    task = await ctx.repo.get_task(task_id)
    if not task:
        raise HTTPException(404)
    form = await request.form()
    envelope = task.get("envelope") or {}
    try:
        prefs = preferences_from_form(
            form, authorized_media=bool(form.get("rights")) or
            bool(envelope.get("media_edit_rights_confirmed")))
    except ValueError:
        return _back(f"/tasks/{task_id}", _tr(request, "web_invalid_options"))
    _, msg = await ctx.ops.rerun_images(task_id, task["tg_user_id"], prefs)
    return _back(f"/tasks/{task_id}", msg)


@router.post("/tasks/{task_id}/authorize/{idx}")
async def authorize_image(request: Request, task_id: str, idx: int) -> RedirectResponse:
    ctx = _ctx(request)
    task = await ctx.repo.get_task(task_id)
    if not task:
        raise HTTPException(404)
    _, msg = await ctx.ops.authorize_image_edit(task_id, task["tg_user_id"], task["tg_chat_id"],
                                                idx)
    return _back(f"/tasks/{task_id}", msg)


@router.get("/api/tasks/{task_id}/status")
async def task_status(request: Request, task_id: str) -> JSONResponse:
    task = await _ctx(request).repo.get_task(task_id)
    if not task:
        raise HTTPException(404)
    return JSONResponse({"status": task["status"], "stage": task.get("stage"),
                         "updated_at": str(task.get("updated_at") or "")})
