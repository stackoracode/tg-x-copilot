"""Issue #15: web compose channel (no Telegram needed)."""

from __future__ import annotations

import base64
import io
import os
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import Recorder, png_bytes
from tg_x_copilot.app_context import AppContext
from tg_x_copilot.bot.telegram import TelegramBot
from tg_x_copilot.config import AppSettings
from tg_x_copilot.image_settings import ImageAction, ImagePreferences
from tg_x_copilot.models import InputEnvelope, MediaKind
from tg_x_copilot.pipeline.media import inspect_image
from tg_x_copilot.pipeline.processor import Pipeline
from tg_x_copilot.pipeline.web_input import (
    WebPage, build_web_envelope, split_urls, validate_uploads,
)
from tg_x_copilot.services.ops import Ops
from tg_x_copilot.services.url_fetch import (
    UrlFetchError, check_url, extract_text, fetch_page,
)
from tg_x_copilot.services.web_inbox import WebInbox
from tg_x_copilot.web.app import create_app

TASK_ID = "a" * 32


def jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "JPEG")
    return buf.getvalue()


# --------------------------------------------------------------------------- envelope


def test_web_envelope_shape():
    images = [inspect_image(png_bytes()), inspect_image(jpeg_bytes())]
    env = build_web_envelope(
        text="Launch details at https://example.com/a?utm_source=x", urls=["https://news.example/b"],
        pages=[WebPage("https://news.example/b", "Full article text")], images=images,
        own_content=False, locale="en-US", market="US")
    assert env.channel == "web" and env.chat_id == 0 and env.user_id == 0
    assert env.message_ids == [1, 2]
    assert [m.message_id for m in env.media] == [1, 2]
    assert all(m.kind == MediaKind.IMAGE_DOCUMENT and m.forwarded is True for m in env.media)
    assert env.message_texts == ["Launch details at https://example.com/a?utm_source=x",
                                 "Full article text"]  # each page is its own "message"
    assert "https://news.example/b" in env.urls and not env.is_direct


def test_web_envelope_own_content_and_url_only():
    env = build_web_envelope(text="", urls=["https://example.com"], pages=[], images=[],
                             own_content=True, locale="en-US", market="US")
    assert env.is_direct and env.message_ids == [1]
    assert env.text == "https://example.com"  # a URL-only submission still reaches the editor


def test_split_urls_dedupes_and_ignores_junk():
    assert split_urls("https://a.example, ftp://x https://a.example\nhttp://b.example") == [
        "https://a.example", "http://b.example"]


def test_validate_uploads_sniffs_content_and_enforces_limits():
    files = [("a.png", png_bytes()), ("evil.png", b"<script>"), ("big.jpg", b"x" * 50),
             ("b.jpg", jpeg_bytes()), ("c.png", png_bytes("blue"))]
    images, problems = validate_uploads(files, max_files=2, max_bytes=40 * 1024)
    assert [i.mime for i in images] == ["image/png", "image/jpeg"]
    assert len(problems) == 3  # not an image, not an image (too small to be one), over count


# --------------------------------------------------------------------------- inbox


def test_inbox_lifecycle(tmp_path):
    inbox = WebInbox(tmp_path)
    inbox.save(TASK_ID, 0, b"zero")
    inbox.save(TASK_ID, 1, b"one")
    assert inbox.load(TASK_ID, [0, 1, 2]) == {0: b"zero", 1: b"one"}
    assert inbox.delete(TASK_ID) and inbox.load(TASK_ID, [0]) == {}


def test_inbox_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        WebInbox(tmp_path).save("../../etc", 0, b"x")


def test_inbox_purge_only_removes_expired(tmp_path):
    inbox = WebInbox(tmp_path)
    inbox.save(TASK_ID, 0, b"old")
    inbox.save("b" * 32, 0, b"new")
    old = time.time() - 30 * 86400
    os.utime(tmp_path / TASK_ID, (old, old))
    assert inbox.purge(14 * 86400) == 1
    assert inbox.load("b" * 32, [0]) == {0: b"new"}


# --------------------------------------------------------------------------- URL fetch


def test_extract_text_keeps_article_drops_chrome():
    html = """<html><head><title>Ignored</title><meta property="og:title" content="Real Title">
    <meta name="description" content="Short summary."><script>var x=1</script></head>
    <body><nav>Home | About</nav><article><h1>Headline</h1><p>First &amp; second
    paragraph.</p><p>Second paragraph.</p></article><footer>© site</footer></body></html>"""
    title, text = extract_text(html)
    assert title == "Real Title"
    assert text.splitlines() == ["Short summary.", "Headline", "First & second paragraph.",
                                 "Second paragraph."]


async def fake_resolver(addresses: dict[str, list[str]]):
    async def resolve(host: str, port: int) -> list[str]:
        return addresses[host]
    return resolve


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin", "http://localhost/", "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.5/", "http://[::1]/", "http://user:pw@example.com/", "file:///etc/passwd",
    "ftp://example.com/", "http://100.64.0.1/",
])
async def test_check_url_blocks_non_public_targets(url):
    resolve = await fake_resolver({"localhost": ["127.0.0.1"], "example.com": ["93.184.216.34"]})
    with pytest.raises(UrlFetchError):
        await check_url(url, resolve)


async def test_check_url_requires_all_addresses_public():
    resolve = await fake_resolver({"mixed.example": ["93.184.216.34", "192.168.1.10"],
                                   "ok.example": ["93.184.216.34"]})
    with pytest.raises(UrlFetchError):
        await check_url("https://mixed.example/", resolve)
    assert await check_url("https://ok.example/x", resolve) == "https://ok.example/x"


async def test_fetch_page_rechecks_redirect_targets():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://internal.example/secret"})
        return httpx.Response(200, text="secret")

    resolve = await fake_resolver({"public.example": ["93.184.216.34"],
                                   "internal.example": ["10.1.2.3"]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UrlFetchError, match="non-public"):
            await fetch_page(client, "http://public.example/", timeout=5, max_bytes=1000,
                             max_chars=1000, resolver=resolve)


async def test_fetch_page_reads_html_and_caps_size():
    body = "<html><body><p>" + "word " * 5000 + "</p></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bin":
            return httpx.Response(200, content=b"\x00", headers={"content-type": "application/zip"})
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    resolve = await fake_resolver({"news.example": ["93.184.216.34"]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await fetch_page(client, "https://news.example/a", timeout=5, max_bytes=2000,
                                max_chars=500, resolver=resolve)
        assert page.text.startswith("word word") and len(page.text) <= 500
        with pytest.raises(UrlFetchError, match="content type"):
            await fetch_page(client, "https://news.example/bin", timeout=5, max_bytes=2000,
                             max_chars=500, resolver=resolve)


# --------------------------------------------------------------------------- pipeline + bot


async def test_processor_loads_web_media_from_inbox_without_telegram(app, settings, tmp_path):
    app.web_inbox = WebInbox(tmp_path)
    app.web_inbox.save(TASK_ID, 0, png_bytes())  # image 1 staged, image 2 expired/missing
    app.telegram = Recorder()
    images = [inspect_image(png_bytes()), inspect_image(jpeg_bytes())]
    env = build_web_envelope(text="Some text for the task", urls=[], pages=[], images=images,
                             own_content=False, locale="en-US", market="US")
    loaded, _ = await Pipeline(app)._load_media(TASK_ID, env, settings, use=True)
    assert [img.idx for img in loaded] == [0]
    assert app.telegram.named("fetch_media") == []  # never touches Telegram
    reasons = [a[3] for a, _ in app.repo.named("update_media_decision")]
    assert reasons and "no longer available" in reasons[0]


async def test_telegram_ignores_web_tasks(app):
    app.repo.returns["get_task"] = {"id": TASK_ID, "tg_chat_id": 0, "status": "draft_ready",
                                    "envelope": {"channel": "web"}, "locale": "en-US"}
    bot = TelegramBot.__new__(TelegramBot)
    bot.app, bot.client = app, Recorder()
    await bot.update_task_status(TASK_ID, "final", text="done")
    await bot.send_draft(TASK_ID)
    assert bot.client.calls == []


# --------------------------------------------------------------------------- intake + ops


async def test_submit_web_stages_media_before_task_exists(app, tmp_path):
    app.web_inbox = WebInbox(tmp_path)
    order: list[str] = []

    async def create_task(env: InputEnvelope, *, task_id: str) -> str:
        order.append("create_task")
        assert WebInbox(tmp_path).load(task_id, [0]), "media must be on disk first"
        return task_id

    app.repo.returns["create_task"] = create_task
    app.workers = Recorder(enqueue=True)
    images = [inspect_image(png_bytes())]
    env = build_web_envelope(text="hello world", urls=[], pages=[], images=images,
                             own_content=False, locale="en-US", market="US")
    prefs = ImagePreferences(authorized_media=True, image_action=ImageAction.RECREATE)
    task_id = await AppContext.submit_web(app, env, prefs, images)  # type: ignore[arg-type]
    assert order == ["create_task"] and app.workers.named("enqueue")
    assert env.media_edit_rights_confirmed and env.image_action == ImageAction.RECREATE
    assert WebInbox(tmp_path).load(task_id, [0])


async def test_submit_web_cleans_up_when_task_creation_fails(app, tmp_path):
    app.web_inbox = WebInbox(tmp_path)
    app.repo.returns["create_task"] = RuntimeError("db down")
    app.workers = Recorder(enqueue=True)
    images = [inspect_image(png_bytes())]
    env = build_web_envelope(text="hello", urls=[], pages=[], images=images, own_content=False,
                             locale="en-US", market="US")
    with pytest.raises(RuntimeError):
        await AppContext.submit_web(app, env, ImagePreferences(), images)  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


def _web_task(status: str) -> dict[str, Any]:
    return {"id": TASK_ID, "status": status, "draft_meta": {}, "envelope": {"channel": "web"},
            "tg_user_id": 0, "locale": "en-US"}


async def test_web_approve_keeps_final_images_and_close_deletes_uploads(app, tmp_path):
    app.web_inbox = WebInbox(tmp_path)
    app.web_inbox.save(TASK_ID, 0, b"img")
    app.storage.returns["release_task"] = 0
    app.repo.returns["get_task"] = _web_task("draft_ready")
    ops = Ops(app)
    assert (await ops.approve(TASK_ID))[0]
    assert app.storage.named("release_task")[-1][1]["kinds"] == ("review",)  # finals stay
    assert app.web_inbox.load(TASK_ID, [0])  # still needed for regenerate
    app.repo.returns["get_task"] = _web_task("approved")
    assert (await ops.publish(TASK_ID))[0]
    assert app.web_inbox.load(TASK_ID, [0]) == {}  # closed: staged uploads deleted


async def test_health_reports_web_only_mode_as_ok(app, settings):
    settings.telegram.enabled = False
    app.db = Recorder()
    ops = Ops(app)
    for name in ("_cpa_models", "_jev_models"):
        setattr(ops, name, Recorder().list)
    results = {r.name: r for r in await ops.test_connections()}
    assert results["telegram"].ok and "web-only" in results["telegram"].detail


# --------------------------------------------------------------------------- routes


def make_client(tmp_path) -> tuple[TestClient, SimpleNamespace]:
    settings = AppSettings(_env_file=None)
    settings.admin.password = settings.admin.password.__class__("pw")
    settings.web.fetch_urls = False
    task = {"id": TASK_ID, "status": "needs_review", "stage": "done", "locale": "en-US",
            "market": "US", "score": None, "route": None, "attempts": 1, "created_at": "now",
            "updated_at": "now", "error": None, "source_text": "src", "jev_result": None,
            "evaluation": None, "tg_user_id": 0, "tg_chat_id": 0,
            "draft_text": "Draft post", "draft_meta": {"problems": [], "review": ["check"],
                                                     "x_length": 10},
            "envelope": {"channel": "web", "urls": [], "forwards": [], "image_action": None,
                         "image_options": {}, "media_edit_rights_confirmed": False}}
    submitted: dict[str, Any] = {}

    async def submit_web(env: InputEnvelope, prefs: ImagePreferences, images: list) -> str:
        submitted.update(env=env, prefs=prefs, images=images)
        return TASK_ID

    prefs_store = Recorder(get=ImagePreferences(), update=ImagePreferences())
    ctx = SimpleNamespace(
        config=SimpleNamespace(current=settings, base=settings),
        i18n=__import__("tg_x_copilot.i18n", fromlist=["I18n"]).I18n(),
        image_preferences=prefs_store,
        hub=SimpleNamespace(http=None, r2=SimpleNamespace(configured=False)),
        repo=Recorder(get_task=task, list_media=[{"idx": 0, "kind": "image_document",
                                                   "decision": "review", "decision_reason": "x"}],
                      list_events=[]),
        ops=Recorder(rerun_images=(True, "queued"), authorize_image_edit=(True, "ok")),
        submit_web=submit_web,
        workers=SimpleNamespace(stats={"queued": 0, "busy": {}, "workers": 1}),
        telegram=None,
    )
    app = create_app(settings)
    app.state.ctx = ctx  # no lifespan: no DB/Telegram started
    token = base64.b64encode(b"admin:pw").decode()
    client = TestClient(app, headers={"Authorization": f"Basic {token}"})
    return client, SimpleNamespace(ctx=ctx, submitted=submitted)


def test_compose_page_renders(tmp_path):
    client, _ = make_client(tmp_path)
    r = client.get("/new")
    assert r.status_code == 200 and 'enctype="multipart/form-data"' in r.text
    assert 'accept="image/*"' in r.text


def test_compose_submit_creates_web_task(tmp_path):
    client, state = make_client(tmp_path)
    r = client.post("/new", data={"text": "Hello from my phone", "urls": "https://example.com",
                                  "rights": "1", "image_action": "recreate"},
                    files=[("images", ("shot.png", png_bytes(), "image/png")),
                           ("images", ("note.txt", b"not an image", "text/plain"))],
                    headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/tasks/{TASK_ID}")
    env, prefs = state.submitted["env"], state.submitted["prefs"]
    assert env.channel == "web" and len(state.submitted["images"]) == 1  # text file refused
    assert prefs.authorized_media and prefs.image_action == ImageAction.RECREATE
    assert "note.txt" in r.headers["location"]  # problem reported back to the operator
    assert state.ctx.image_preferences.named("update")  # remembered for regenerate


def test_compose_submit_with_nothing_goes_back(tmp_path):
    client, state = make_client(tmp_path)
    r = client.post("/new", data={"text": "  "}, headers={"Origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/new")
    assert "env" not in state.submitted


def test_compose_rejects_cross_site_and_anonymous(tmp_path):
    client, state = make_client(tmp_path)
    assert client.post("/new", data={"text": "x"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    anon = TestClient(client.app)
    assert anon.get("/new").status_code == 401
    assert "env" not in state.submitted


def test_task_page_has_copy_tools_and_live_status(tmp_path):
    client, _ = make_client(tmp_path)
    r = client.get(f"/tasks/{TASK_ID}")
    assert r.status_code == 200
    assert 'id="copy"' in r.text and f'action="/tasks/{TASK_ID}/images"' in r.text
    status = client.get(f"/api/tasks/{TASK_ID}/status").json()
    assert status["status"] == "needs_review"


def test_image_rerun_route_is_not_shadowed_by_admin_actions(tmp_path):
    client, state = make_client(tmp_path)
    r = client.post(f"/tasks/{TASK_ID}/images", data={"image_action": "info_card"},
                    headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    (task_id, user_id, prefs), _ = state.ctx.ops.named("rerun_images")[0]
    assert task_id == TASK_ID and user_id == 0 and prefs.image_action == ImageAction.INFO_CARD
    r = client.post(f"/tasks/{TASK_ID}/authorize/0", headers={"Origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303 and state.ctx.ops.named("authorize_image_edit")


def test_invalid_image_options_are_reported(tmp_path):
    client, state = make_client(tmp_path)
    r = client.post("/new", data={"text": "x", "custom_options": "1",
                                  "flag": ["redesign", "minimal_changes"]},
                    headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/new")
    assert "env" not in state.submitted


def test_dashboard_renders_in_web_only_mode(tmp_path):
    client, state = make_client(tmp_path)
    state.ctx.config.base.telegram.enabled = False
    state.ctx.repo.returns.update(status_counts={}, stored_bytes=0, list_tasks=[])
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/new"' in r.text and "web-only" in r.text


def test_web_channel_can_be_disabled(tmp_path):
    client, state = make_client(tmp_path)
    state.ctx.config.base.web.enabled = False
    assert client.get("/new").status_code == 404
