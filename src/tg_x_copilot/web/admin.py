"""Minimal server-rendered admin UI (HTTP Basic auth). Bind to 127.0.0.1 and reach it through an
SSH tunnel: `ssh -L 8080:127.0.0.1:8080 vps`."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from ..config import EDITABLE_KEYS, get_path
from ..logging_setup import mask

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_basic = HTTPBasic()


def _ctx(request: Request) -> Any:
    return request.app.state.ctx


def require_admin(request: Request, creds: HTTPBasicCredentials = Depends(_basic)) -> str:
    cfg = _ctx(request).config.base.admin
    password = cfg.password.get_secret_value()
    if not password:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Set ADMIN__PASSWORD to enable the admin UI.")
    ok_user = secrets.compare_digest(creds.username.encode(), cfg.username.encode())
    ok_pass = secrets.compare_digest(creds.password.encode(), password.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized",
                            headers={"WWW-Authenticate": "Basic"})
    return creds.username


router = APIRouter(dependencies=[Depends(require_admin)])


def _render(request: Request, name: str, **data: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"flash": request.query_params.get("msg"),
                                                      **data})


def _back(url: str, msg: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"{url}?msg={quote(msg)}", status_code=303)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, status_filter: str | None = None) -> HTMLResponse:
    ctx = _ctx(request)
    return _render(
        request, "dashboard.html",
        counts=await ctx.repo.status_counts(),
        stored_mb=round(await ctx.repo.stored_bytes() / 1e6, 1),
        budget_gb=round(ctx.config.current.storage.budget_bytes / 1e9, 1),
        tasks=await ctx.repo.list_tasks(limit=100, status=status_filter),
        workers=ctx.workers.stats,
        status_filter=status_filter,
        telegram=bool(ctx.telegram and ctx.telegram.connected),
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: str) -> HTMLResponse:
    ctx = _ctx(request)
    task = await ctx.repo.get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    media = await ctx.repo.list_media(task_id)
    r2 = ctx.hub.r2
    for m in media:
        key = m.get("asset_key")
        m["asset_url"] = (r2.public_url(key) or r2.presign_get(key)) if key and r2.configured \
            else None
    return _render(request, "task.html", task=task, media=media,
                   events=await ctx.repo.list_events(task_id))


@router.post("/tasks/{task_id}/{action}")
async def task_action(request: Request, task_id: str, action: str) -> RedirectResponse:
    ops = _ctx(request).ops
    handlers = {"approve": ops.approve, "reject": ops.reject, "regenerate": ops.regenerate}
    if action not in handlers:
        raise HTTPException(404)
    _, msg = await handlers[action](task_id)
    return _back(f"/tasks/{task_id}", msg)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    ctx = _ctx(request)
    base = ctx.config.base
    bootstrap = {
        "db": f"{base.db.user}@{base.db.host}:{base.db.port}/{base.db.database}",
        "telegram": f"api_id={base.telegram.api_id} allowed_users={base.telegram.allowed_user_ids}",
        "concurrency": base.concurrency.model_dump(),
        "locales": ctx.i18n.codes,
        "credentials (env-only, values never shown)": ctx.config.credentials_status(),
    }
    return _render(request, "settings.html", rows=ctx.config.view(),
                   models=await ctx.repo.list_models(), bootstrap=bootstrap)


@router.post("/settings")
async def settings_save(request: Request) -> RedirectResponse:
    ctx = _ctx(request)
    form = await request.form()
    changes: dict[str, Any] = {}
    for key in EDITABLE_KEYS:  # API keys are env-only and not part of this form
        if key not in form:
            continue
        raw = str(form[key])
        current = get_path(ctx.config.current, key)
        try:
            new = ctx.config.parse_value(key, raw, current)
        except ValueError as exc:
            return _back("/settings", f"Invalid value for {key}: {exc}")
        if new != current and not (current is not None and str(current) == str(new)):
            changes[key] = new
    for key in EDITABLE_KEYS:  # unchecked checkboxes are absent from the form
        if f"{key}__bool" in form and key not in form and get_path(ctx.config.current, key):
            changes[key] = False
    if not changes:
        return _back("/settings", "No changes.")
    try:
        keys = await ctx.config.update(changes)
    except (ValueError, ValidationError) as exc:
        return _back("/settings", mask(f"Invalid settings: {exc}")[:500])
    return _back("/settings", f"Saved: {', '.join(keys)}")


@router.post("/settings/reset/{key}")
async def settings_reset(request: Request, key: str) -> RedirectResponse:
    if key not in EDITABLE_KEYS:
        raise HTTPException(404)
    await _ctx(request).config.reset(key)
    return _back("/settings", f"Reset {key} to env default.")


@router.post("/actions/refresh-models")
async def refresh_models(request: Request) -> RedirectResponse:
    summary = await _ctx(request).ops.refresh_models()
    return _back("/settings", f"Models refreshed: {summary}")


@router.get("/actions/health", response_class=HTMLResponse)
async def health(request: Request) -> HTMLResponse:
    results = await _ctx(request).ops.test_connections()
    return _render(request, "health.html", results=results)


@router.get("/api/tasks")
async def api_tasks(request: Request, status_filter: str | None = None,
                    limit: int = 50) -> JSONResponse:
    rows = await _ctx(request).repo.list_tasks(limit=min(limit, 500), status=status_filter)
    return JSONResponse(jsonable(rows))


@router.get("/api/tasks/{task_id}")
async def api_task(request: Request, task_id: str) -> JSONResponse:
    ctx = _ctx(request)
    task = await ctx.repo.get_task(task_id)
    if not task:
        raise HTTPException(404)
    return JSONResponse(jsonable({"task": task, "media": await ctx.repo.list_media(task_id),
                                  "events": await ctx.repo.list_events(task_id)}))


def jsonable(v: Any) -> Any:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(v)
