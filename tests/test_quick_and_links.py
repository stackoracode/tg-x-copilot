"""Quick create (always post + image, both channels) and the multi-strategy link reader."""

from __future__ import annotations

import base64
import sys
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from conftest import Recorder, png_bytes
from fastapi.testclient import TestClient
from test_image_tools import button_data

from tg_x_copilot.app_context import submit_envelope
from tg_x_copilot.bot.image_tools import keyboard
from tg_x_copilot.bot.telegram import TelegramBot
from tg_x_copilot.config import AppSettings, is_secret_key
from tg_x_copilot.i18n import I18n
from tg_x_copilot.image_settings import ImagePreferences, WorkflowMode
from tg_x_copilot.models import InputEnvelope, MediaKind, SourceMedia, TaskStatus
from tg_x_copilot.pipeline.links import attach_linked_pages, links_to_read
from tg_x_copilot.pipeline.processor import Pipeline
from tg_x_copilot.pipeline.quick import QuickDraft, strip_command
from tg_x_copilot.services.link_reader import LinkReader, ReadResult
from tg_x_copilot.services.url_fetch import PageContent, block_reason
from tg_x_copilot.web.app import create_app

TASK_ID = "b" * 32
ARTICLE = "<html><head><title>Launch</title></head><body><article><p>" + \
          "The rocket launched on 3 May carrying 12 satellites into orbit. " * 10 + \
          "</p></article></body></html>"


def quick_env(**kw: Any) -> InputEnvelope:
    base = dict(chat_id=1, user_id=7, message_ids=[10], text="New coffee shop opens downtown",
                message_texts=["New coffee shop opens downtown"], quick=True)
    return InputEnvelope(**{**base, **kw})


def task_of(env: InputEnvelope, **kw: Any) -> dict[str, Any]:
    return {"id": TASK_ID, "attempts": 0, "envelope": env.model_dump(mode="json"),
            "locale": "en-US", "status": "processing", **kw}


def saved(app) -> tuple[TaskStatus, str, dict[str, Any]]:
    (task_id, status, text, meta), kw = app.repo.named("save_draft")[-1]
    assert kw["expected"] == (TaskStatus.PROCESSING,)
    return status, text, meta


@pytest.fixture
def quick_app(app):
    app.storage.returns["persist"] = "assets/aa/x.jpg"
    app.hub.cpa.returns.update(
        chat_json=QuickDraft(post="Fresh beans, fresh start ☕", image_prompt="A cozy cafe",
                             alt_text="Cafe"),
        images_generate=png_bytes("blue"), images_edit=png_bytes("green"))
    return app


# --------------------------------------------------------------------------- quick create


async def test_quick_text_only_always_returns_post_and_image(quick_app, settings):
    env = quick_env()
    await Pipeline(quick_app)._process_claimed(TASK_ID, task_of(env))
    status, text, meta = saved(quick_app)
    assert status == TaskStatus.DRAFT_READY and text.startswith("Fresh beans")
    assert meta["quick"] and meta["image_prompt"] == "A cozy cafe"
    assert [m["decision"] for m in meta["media"]] == ["generate"]
    assert meta["media"][0]["asset_kind"] == "final" and meta["media"][0]["ai_generated"]
    cpa = quick_app.hub.cpa
    assert len(cpa.named("images_generate")) == 1 and not cpa.named("images_edit")
    (model, messages, schema), _ = cpa.named("chat_json")[0]
    assert model == settings.models.text_model and schema is QuickDraft
    # bypassed: no Jev, no evaluation, no QC call
    assert not quick_app.hub.jev.calls and len(cpa.named("chat_json")) == 1
    assert quick_app.repo.named("save_triage") == [] and quick_app.repo.named("save_evaluation") == []


async def test_quick_uses_attached_photo_as_reference(quick_app, settings):
    quick_app.telegram = Recorder(fetch_media={10: png_bytes("red")})
    env = quick_env(media=[SourceMedia(message_id=10, kind=MediaKind.PHOTO, forwarded=True)],
                    is_direct=False)
    await Pipeline(quick_app)._process_claimed(TASK_ID, task_of(env))
    status, _, meta = saved(quick_app)
    assert status == TaskStatus.DRAFT_READY
    assert [m["decision"] for m in meta["media"]] == ["omit", "generate"]  # source not delivered
    (model, messages, _), _ = quick_app.hub.cpa.named("chat_json")[0]
    assert model == settings.models.vision_model
    assert any(part.get("type") == "image_url" for part in messages[1]["content"])
    (args, _), = quick_app.hub.cpa.named("images_edit")
    assert args[2] == png_bytes("red")  # forwarded photo used without any rights gate
    assert not quick_app.hub.cpa.named("images_generate")


async def test_quick_falls_back_to_generation_when_edit_keeps_failing(quick_app):
    quick_app.telegram = Recorder(fetch_media={10: png_bytes("red")})
    quick_app.hub.cpa.returns["images_edit"] = RuntimeError("edits unsupported")
    env = quick_env(media=[SourceMedia(message_id=10, kind=MediaKind.PHOTO)])
    await Pipeline(quick_app)._process_claimed(TASK_ID, task_of(env))
    status, _, meta = saved(quick_app)
    assert status == TaskStatus.DRAFT_READY and meta["media"][-1]["decision"] == "generate"
    assert len(quick_app.hub.cpa.named("images_edit")) == 2
    assert len(quick_app.hub.cpa.named("images_generate")) == 1


async def test_quick_image_failure_still_delivers_the_post(quick_app):
    quick_app.hub.cpa.returns["images_generate"] = RuntimeError("upstream down")
    quick_app.telegram = Recorder(get_task=None)
    await Pipeline(quick_app)._process_claimed(TASK_ID, task_of(quick_env()))
    status, text, meta = saved(quick_app)
    assert status == TaskStatus.NEEDS_REVIEW and text.startswith("Fresh beans")
    assert meta["media"][0]["failure_stage"] == "IMAGE2" and meta["review"]
    assert meta["problems"] == []  # approvable as-is
    assert quick_app.telegram.named("send_draft")  # delivered anyway


async def test_quick_shortens_an_over_long_post(quick_app):
    long = QuickDraft(post="x" * 400, image_prompt="p")
    short = QuickDraft(post="Short and sweet", image_prompt="p")
    replies = iter([long, short])
    quick_app.hub.cpa.returns["chat_json"] = lambda *a, **k: next(replies)
    await Pipeline(quick_app)._process_claimed(TASK_ID, task_of(quick_env()))
    _, text, meta = saved(quick_app)
    assert text == "Short and sweet" and not meta["warnings"]
    retry_messages = quick_app.hub.cpa.named("chat_json")[1][0][1]
    assert "280" in retry_messages[-1]["content"]


def test_strip_command_removes_telegram_prefix():
    env = strip_command(quick_env(text="/quick@mybot  launch day!",
                                  message_texts=["/quick launch day!"]))
    assert env.text == "launch day!" and env.message_texts == ["launch day!"]


async def test_quick_image_rerun_keeps_post(quick_app):
    env = quick_env(processing_mode="images_only")
    task = task_of(env, draft_text="Existing post",
                   draft_meta={"quick": True, "image_prompt": "A cozy cafe", "text_warnings": []})
    await Pipeline(quick_app)._process_claimed(TASK_ID, task)
    status, text, meta = saved(quick_app)
    assert text == "Existing post" and status == TaskStatus.DRAFT_READY
    (args, _), = quick_app.hub.cpa.named("images_generate")
    assert args[1].startswith("A cozy cafe")
    assert not quick_app.hub.cpa.named("chat_json")  # no new text


async def test_submit_marks_quick_profile_tasks(app):
    app.repo.returns["create_task"] = TASK_ID
    app.workers = Recorder()
    env = InputEnvelope(chat_id=1, user_id=7, message_ids=[1], text="hello")
    await submit_envelope(app, env, ImagePreferences(workflow_mode=WorkflowMode.QUICK))
    (stored,), _ = app.repo.named("create_task")[0]
    assert stored.quick and stored.workflow_mode == WorkflowMode.QUICK
    env2 = InputEnvelope(chat_id=1, user_id=7, message_ids=[2], text="hello")
    await submit_envelope(app, env2, ImagePreferences())
    assert not app.repo.named("create_task")[1][0][0].quick


# --------------------------------------------------------------------------- telegram


def make_bot(app) -> TelegramBot:
    bot = TelegramBot.__new__(TelegramBot)
    bot.app, bot.client = app, Recorder(send_message=SimpleNamespace(id=99))
    bot._allowed = {7}
    return bot


def capture_intake(app) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def intake(chat_id, user_id, messages, **kwargs):
        calls.append({"messages": messages, **kwargs})

    app.intake = intake
    return calls


async def test_quick_command_with_text_forces_quick_task(app):
    app.image_preferences = Recorder(get=ImagePreferences())
    calls = capture_intake(app)
    bot = make_bot(app)
    message = SimpleNamespace(id=5, raw_text="/quick hello", grouped_id=None, media=None)
    event = SimpleNamespace(message=message, raw_text="/quick hello", chat_id=7, sender_id=7)
    await bot._quick_command(event)
    assert calls[0]["preferences"].workflow_mode == WorkflowMode.QUICK


async def test_bare_quick_command_toggles_profile(app):
    state = {"prefs": ImagePreferences()}

    async def update(user_id, mutate):
        state["prefs"] = mutate(state["prefs"])
        return state["prefs"]

    app.image_preferences = SimpleNamespace(update=update)
    bot = make_bot(app)
    replies: list[str] = []

    async def respond(text, **kw):
        replies.append(text)

    event = SimpleNamespace(message=SimpleNamespace(grouped_id=None, media=None),
                            raw_text="/quick", sender_id=7, respond=respond)
    await bot._quick_command(event)
    assert state["prefs"].workflow_mode == WorkflowMode.QUICK and "ON" in replies[-1]
    await bot._quick_command(event)
    assert state["prefs"].workflow_mode == WorkflowMode.MANUAL and "OFF" in replies[-1]


async def test_album_captioned_quick_is_quick(app):
    app.image_preferences = Recorder(get=ImagePreferences())
    calls = capture_intake(app)
    bot = make_bot(app)
    msgs = [SimpleNamespace(id=1, raw_text="/quick make it pop"), SimpleNamespace(id=2, raw_text="")]
    await bot._receive(SimpleNamespace(chat_id=7, sender_id=7), msgs)
    assert calls[0]["preferences"].workflow_mode == WorkflowMode.QUICK


def test_image_tools_menu_offers_quick_toggle():
    i18n = I18n()
    rows = keyboard("-", ImagePreferences(workflow_mode=WorkflowMode.QUICK),
                    lambda key, **kw: i18n.t("en-US", key, **kw))
    first = rows[0][0]
    assert "Quick create" in first.text and first.text.startswith("☑")
    assert button_data(first) == b"it:-:w:manual"


# --------------------------------------------------------------------------- link selection


def test_links_to_read_rules(settings):
    web = settings.web
    short = InputEnvelope(chat_id=1, user_id=1, message_ids=[1], text="see https://a.example/x",
                          urls=["https://a.example/x?utm_source=tg", "https://t.me/chan/1"])
    assert links_to_read(short, web) == ["https://a.example/x"]  # tracking + t.me dropped
    long = short.model_copy(update={"text": "y" * 400 + " https://a.example/x"})
    assert links_to_read(long, web) == []
    web.fetch_telegram_links = "always"
    assert links_to_read(long, web) == ["https://a.example/x"]
    web.fetch_telegram_links = "off"
    assert links_to_read(short, web) == []
    page = short.model_copy(update={"channel": "web"})
    assert links_to_read(page, web) == []  # web: only when the compose box asked for it
    assert links_to_read(page.model_copy(update={"fetch_links": True}), web) == ["https://a.example/x"]
    many = page.model_copy(update={"fetch_links": True,
                                   "urls": [f"https://a.example/{i}" for i in range(9)]})
    assert len(links_to_read(many, web)) == web.fetch_max_links


async def test_attach_linked_pages_adds_messages_and_notes(app):
    pages = {
        "https://ok.example/a": ReadResult("https://ok.example/a",
                                           PageContent("https://ok.example/a", "T", "body " * 60)),
        "https://ai.example/b": ReadResult("https://ai.example/b",
                                           PageContent("https://ai.example/b", "", "ai " * 80, method="llm")),
        "https://bad.example/c": ReadResult("https://bad.example/c"),
    }

    async def read(url, *, allow_ai=True):
        return pages[url]

    app.links = SimpleNamespace(read=read)
    events: list[tuple] = []

    async def event(*a, **k):
        events.append(a)

    env = InputEnvelope(chat_id=1, user_id=1, message_ids=[1], text="links", message_texts=["links"],
                        message_urls=[list(pages)], urls=list(pages))
    out, notes, ai = await attach_linked_pages(TASK_ID, env, app, event)
    assert out.message_texts[0] == "links" and len(out.message_texts) == 3
    assert out.message_urls[1:] == [["https://ok.example/a"], ["https://ai.example/b"]]
    assert out.text.startswith("links\n\nT") and ai
    assert any("AI model" in n for n in notes) and any("bad.example" in n for n in notes)
    assert len(events) == 3 and env.message_texts == ["links"]  # stored envelope untouched


# --------------------------------------------------------------------------- link reader


def reader_app(handler, **web: Any) -> SimpleNamespace:
    settings = AppSettings(_env_file=None)
    for key, value in web.items():
        setattr(settings.web, key, value)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SimpleNamespace(config=SimpleNamespace(current=settings), hub=SimpleNamespace(http=http,
                           cpa=Recorder()), limits=SimpleNamespace(text=_NullLock()))


class _NullLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def public(host: str, port: int) -> list[str]:
    return ["10.0.0.1"] if host.startswith("internal") else ["93.184.216.34"]


async def test_block_page_falls_through_to_reader():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "r.jina.ai":
            assert request.headers["accept"] == "application/json"
            return httpx.Response(200, json={"data": {"title": "Real title",
                                                      "content": "Menu\n\n\n\n\n  Real article. " * 40}})
        return httpx.Response(200, text="<html><body><p>当前环境异常，完成验证后即可继续访问。</p></body></html>",
                              headers={"content-type": "text/html"})

    app = reader_app(handler, fetch_strategies=["direct", "reader"])
    result = await LinkReader(app, resolver=public).read("https://mp.weixin.qq.com/s/abc")
    assert result.page and result.page.method == "reader" and result.page.title == "Real title"
    assert "\n\n\n" not in result.page.text
    assert [a.method for a in result.attempts] == ["direct", "reader"]
    assert "环境异常" in result.attempts[0].detail
    # direct tried a desktop browser, then a phone/WeChat UA, before giving up
    assert sum("weixin" in u and "jina" not in u for u in seen) == 2


async def test_direct_retries_as_phone():
    def handler(request: httpx.Request) -> httpx.Response:
        if "MicroMessenger" in request.headers["user-agent"]:
            return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})
        return httpx.Response(200, text="<p>Please enable JavaScript</p>",
                              headers={"content-type": "text/html"})

    app = reader_app(handler, fetch_strategies=["direct"])
    result = await LinkReader(app, resolver=public).read("https://news.example/a")
    assert result.page.method == "direct" and "12 satellites" in result.page.text


async def test_reader_401_explains_the_key(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "blocked"})

    app = reader_app(handler, fetch_strategies=["reader"])
    result = await LinkReader(app, resolver=public).read("https://news.example/a")
    assert result.page is None and "WEB__FETCH_READER_API_KEY" in result.summary


async def test_internal_urls_never_reach_any_strategy():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    app = reader_app(handler)
    result = await LinkReader(app, resolver=public).read("https://internal.example/admin")
    assert result.page is None and result.attempts[0].method == "check"


async def test_llm_strategy_only_when_configured_and_allowed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    app = reader_app(handler, fetch_strategies=["direct", "llm"])
    reader = LinkReader(app, resolver=public)
    assert reader.available() == ["direct"]  # no model configured
    app.config.current.web.fetch_llm_model = "browse-model"
    app.hub.cpa.returns["chat_json"] = lambda model, messages, schema, **kw: schema(
        ok=True, title="T", text="Page body. " * 40)
    assert reader.available(allow_ai=False) == ["direct"]
    result = await reader.read("https://news.example/a")
    assert result.page.method == "llm" and result.page.ai_retrieved
    (model, messages, _), _ = app.hub.cpa.named("chat_json")[0]
    assert model == "browse-model" and "https://news.example/a" in messages[1]["content"]
    app.hub.cpa.returns["chat_json"] = lambda model, messages, schema, **kw: schema(ok=False)
    assert (await reader.read("https://news.example/a")).page is None


async def test_browser_strategy_reports_missing_playwright(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)  # import fails
    app = reader_app(lambda r: httpx.Response(500), fetch_strategies=["browser"])
    result = await LinkReader(app, resolver=public).read("https://news.example/a")
    assert result.page is None and "pip install playwright" in result.summary


async def test_short_clean_page_is_kept_as_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<p>Short notice: office closed Friday.</p>",
                              headers={"content-type": "text/html"})

    app = reader_app(handler, fetch_strategies=["direct"])
    result = await LinkReader(app, resolver=public).read("https://news.example/a")
    assert result.page and "office closed" in result.page.text


def test_block_reason_ignores_articles_that_mention_captchas():
    long = PageContent("u", "How CAPTCHA works", "x" * 2000)
    assert block_reason(long) is not None  # marker in the title
    article = PageContent("u", "Security news", "A new captcha bypass was found. " + "y" * 2000)
    assert block_reason(article) is None
    assert block_reason(PageContent("u", "Just a moment...", "")).startswith("blocked")


async def test_meta_charset_is_honoured():
    body = ('<html><head><meta charset="gbk"><title>新闻</title></head><body><p>'
            + "中文内容测试。" * 50 + "</p></body></html>").encode("gbk")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    app = reader_app(handler, fetch_strategies=["direct"])
    result = await LinkReader(app, resolver=public).read("https://cn.example/a")
    assert "中文内容测试" in result.page.text


def test_reader_key_is_secret():
    assert is_secret_key("web.fetch_reader_api_key")


# --------------------------------------------------------------------------- web


def make_client(task: dict[str, Any] | None = None):
    settings = AppSettings(_env_file=None)
    settings.admin.password = settings.admin.password.__class__("pw")
    submitted: dict[str, Any] = {}

    async def submit_web(env, prefs, images):
        submitted.update(env=env, prefs=prefs, images=images)
        return TASK_ID

    async def read(url, *, allow_ai=True):
        submitted.setdefault("read", []).append((url, allow_ai))
        return ReadResult(url, PageContent(url, "Title", "Body " * 50, method="browser"))

    ctx = SimpleNamespace(
        config=SimpleNamespace(current=settings, base=settings), i18n=I18n(),
        image_preferences=Recorder(get=ImagePreferences(workflow_mode=WorkflowMode.QUICK),
                                   update=ImagePreferences()),
        hub=SimpleNamespace(http=None, r2=SimpleNamespace(configured=False)),
        repo=Recorder(get_task=task, list_media=[], list_events=[]),
        ops=Recorder(rerun_images=(True, "queued")), submit_web=submit_web,
        links=SimpleNamespace(read=read), telegram=None,
        workers=SimpleNamespace(stats={"queued": 0, "busy": {}, "workers": 1}))
    app = create_app(settings)
    app.state.ctx = ctx
    token = base64.b64encode(b"admin:pw").decode()
    return TestClient(app, headers={"Authorization": f"Basic {token}",
                                    "Origin": "http://testserver"}), submitted


def test_compose_page_defaults_to_remembered_quick_mode():
    client, _ = make_client()
    r = client.get("/new")
    assert 'name="mode" value="quick" checked' in r.text and 'id="test-links"' in r.text
    assert 'class="card quick"' in r.text


def test_compose_quick_submission():
    client, submitted = make_client()
    r = client.post("/new", data={"mode": "quick", "text": "Launch day",
                                  "instructions": "neon style", "urls": "https://a.example/x",
                                  "fetch_pages": "1"}, follow_redirects=False)
    assert r.status_code == 303
    env, prefs = submitted["env"], submitted["prefs"]
    assert prefs.workflow_mode == WorkflowMode.QUICK
    assert env.quick_instructions == "neon style" and env.fetch_links and not env.fetch_ai
    assert "read" not in submitted  # pages are read by the worker, not in the request


def test_link_preview_endpoint():
    client, submitted = make_client()
    r = client.post("/api/links/preview", json={"urls": "see https://a.example/x and ftp://no",
                                                "allow_ai": True})
    data = r.json()["results"]
    assert r.status_code == 200 and len(data) == 1
    assert data[0]["ok"] and data[0]["method"] == "browser" and data[0]["title"] == "Title"
    assert submitted["read"] == [("https://a.example/x", True)]
    blocked = client.post("/api/links/preview", json={"urls": "https://a.example"},
                          headers={"Origin": "https://evil.example"})
    assert blocked.status_code == 403


def test_quick_task_page_offers_new_image():
    task = {"id": TASK_ID, "status": "draft_ready", "stage": "done", "locale": "en-US",
            "market": "US", "score": None, "route": None, "attempts": 1, "created_at": "now",
            "updated_at": "now", "error": None, "source_text": "src", "jev_result": None,
            "evaluation": None, "tg_user_id": 0, "tg_chat_id": 0, "draft_text": "Post",
            "draft_meta": {"quick": True, "image_prompt": "A neon city", "problems": [],
                           "review": []},
            "envelope": {"channel": "web", "quick": True, "urls": [], "forwards": [],
                         "image_options": {}}}
    client, _ = make_client(task)
    r = client.get(f"/tasks/{TASK_ID}")
    assert r.status_code == 200 and "New image" in r.text and "A neon city" in r.text
    assert 'name="image_action"' not in r.text  # quick tasks don't expose the action menu
