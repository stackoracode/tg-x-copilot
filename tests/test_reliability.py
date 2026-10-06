"""Targeted reliability tests: Jev fallback, non-fatal media/R2, image QC, PyMySQL threading,
forward grouping, env-only keys, review of LLM background facts, atomic claims, retention."""

from __future__ import annotations

import asyncio
import itertools
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import Recorder, png_bytes
from tg_x_copilot.bot.telegram import TelegramBot
from tg_x_copilot.clients.http import UpstreamError
from tg_x_copilot.config import EDITABLE_KEYS, AppSettings, is_secret_key
from tg_x_copilot.db import pool as db_pool
from tg_x_copilot.db.repo import Repository
from tg_x_copilot.models import (
    Evaluation, ImageAnalysis, ImageDecision, ImageQC, InputEnvelope, MediaKind, RewriteResult,
    SourceMedia, TaskStatus, VerifiedFact, VerifiedFacts,
)
from tg_x_copilot.pipeline.image_qc import qc_verdict
from tg_x_copilot.pipeline.limits import Limits
from tg_x_copilot.pipeline.media import inspect_image
from tg_x_copilot.pipeline.processor import ImageOutcome, LoadedImage, Pipeline
from tg_x_copilot.services.config_service import ConfigService
from tg_x_copilot.services.ops import Ops

TEXT = "The city council approved a new downtown bike lane network on Monday, 12 km in total."


def envelope(**kw: Any) -> InputEnvelope:
    return InputEnvelope(chat_id=5, user_id=1, message_ids=[10], text=TEXT, **kw)


# --------------------------------------------------------------------------- 1. Jev optional


async def test_jev_error_falls_back_to_main_llm(app):
    app.hub.jev.returns["ask"] = UpstreamError("jev", 529, "Overloaded")
    t = await Pipeline(app)._triage("t1", envelope(), app.i18n.get("en-US"))
    assert t.route == "proceed" and t.escalated
    assert "Jev unavailable" in " ".join(t.reasons)
    events = app.repo.named("add_event")
    assert any(k.get("level") == "warning" for _, k in events)


async def test_jev_timeout_falls_back_to_main_llm(app):
    async def slow(*_: Any, **__: Any) -> None:
        await asyncio.sleep(5)

    app.hub.jev.returns["ask"] = slow
    app.config.current.jev.budget_seconds = 0.05
    t = await asyncio.wait_for(Pipeline(app)._triage("t1", envelope(), app.i18n.get("en-US")), 2)
    assert t.route == "proceed" and t.escalated and "Jev unavailable" in " ".join(t.reasons)


@pytest.mark.parametrize("disable", ["enabled", "configured"])
async def test_jev_disabled_or_unconfigured_is_never_called(app, disable):
    if disable == "enabled":
        app.config.current.jev.enabled = False
    else:
        app.hub.jev.configured = False
    t = await Pipeline(app)._triage("t1", envelope(), app.i18n.get("en-US"))
    assert t.route == "proceed" and t.escalated
    assert app.hub.jev.named("ask") == []


# --------------------------------------------------------------------------- 2. media non-fatal


async def test_media_download_failure_downgrades_to_review(app, settings):
    app.telegram = Recorder(fetch_media=ConnectionError("telegram down"))
    env = envelope(media=[SourceMedia(message_id=10, kind=MediaKind.PHOTO, size=100)])
    images, _ = await Pipeline(app)._load_media("t1", env, settings, use=True)
    assert images == []
    decisions = app.repo.named("update_media_decision")
    assert decisions and decisions[0][0][2] == ImageDecision.REVIEW.value
    assert "Media download failed" in decisions[0][0][3]


async def test_r2_upload_failure_downgrades_to_review(app, settings):
    app.storage.returns["persist"] = RuntimeError("R2 503")
    blob = inspect_image(png_bytes())
    outcome = ImageOutcome(0, ImageDecision.KEEP, "Own media.", output=blob)
    results = await Pipeline(app)._persist("t1", [], [outcome], {}, settings)
    assert results[0].decision == ImageDecision.REVIEW
    assert results[0].asset_key is None
    assert "R2 upload failed" in results[0].reason


def _bot(app: SimpleNamespace) -> TelegramBot:
    bot = TelegramBot.__new__(TelegramBot)
    bot.app = app
    bot.client = Recorder()
    bot._allowed = {1}
    return bot


async def test_text_draft_is_sent_even_if_r2_fetch_fails(app):
    app.repo.returns.update(
        get_task={"id": "t" * 32, "tg_chat_id": 5, "status": "draft_ready", "score": 0.8,
                  "draft_text": "Final post text", "draft_meta": {"x_length": 15}},
        list_media=[{"idx": 0, "asset_key": "assets/ab/abc.png", "asset_kind": "final",
                     "decision": "keep", "ai_generated": 0}],
    )
    app.hub.r2.returns["get_object"] = ConnectionError("R2 down")
    bot = _bot(app)
    await bot.send_draft("t" * 32)
    sent = [a[1] for a, _ in bot.client.named("send_message")]
    assert "Final post text" in sent
    assert any("Images unavailable" in s for s in sent)
    assert bot.client.named("send_file") == []


# --------------------------------------------------------------------------- 3. image QC


def qc(**kw: Any) -> ImageQC:
    base = dict(passed=True, text_consistent=True, numbers_consistent=True,
                dates_consistent=True, names_consistent=True, brands_consistent=True, identifiers_consistent=True, readability_ok=True, density_consistent=True, people_consistent=True,
                watermarks_ok=True, facts_consistent=True, language_consistent=True, rendered_text="", issues=[])
    base.update(kw)
    return ImageQC(**base)


def test_qc_verdict_passes_clean_result():
    assert qc_verdict(qc(rendered_text="12 km of bike lanes"), allowed_texts=[TEXT])[0]


@pytest.mark.parametrize("bad", [
    {"numbers_consistent": False}, {"people_consistent": False}, {"watermarks_ok": False},
    {"names_consistent": False}, {"brands_consistent": False}, {"dates_consistent": False}, {"facts_consistent": False},
    {"text_consistent": False}, {"language_consistent": False}, {"issues": ["logo added"]}, {"passed": False},
    {"rendered_text": "Opening 2027, 40 km"},  # numbers not in the reference
])
def test_qc_verdict_fails_on_any_inconsistency(bad):
    passed, reason = qc_verdict(qc(**bad), allowed_texts=[TEXT])
    assert not passed and reason


def _image_case(app):
    analysis = ImageAnalysis(description="bike lane map", image_type="illustration",
                             relevance=0.9)
    blob = inspect_image(png_bytes())
    img = LoadedImage(0, blob, SourceMedia(message_id=10, kind=MediaKind.PHOTO))
    app.hub.cpa.returns["images_generate"] = png_bytes("blue", (80, 60))
    rewrite = RewriteResult(post="New bike lanes: 12 km downtown.", hook="New bike lanes",
                            added_value="context", image_brief="map")
    evaluation = Evaluation(suitable=True, value_score=0.8, key_facts=["12 km of lanes"])
    return analysis, img, rewrite, evaluation


async def _run_images(app, settings, analysis, img, rewrite, evaluation):
    env = envelope(forwards=[{"chat_id": -100999}])  # third-party -> regenerate
    return await Pipeline(app)._process_images(
        "t1", env, [img], {0: analysis}, rewrite, evaluation, app.i18n.get("en-US"), settings,
        verified_facts=VerifiedFacts(facts=[VerifiedFact(text=TEXT, evidence=TEXT)]))


async def test_image2_output_failing_qc_is_review(app, settings):
    case = _image_case(app)
    app.hub.cpa.returns["chat_json"] = qc(passed=False, people_consistent=False,
                                         issues=["realistic face of a public figure"])
    [out] = await _run_images(app, settings, *case)
    assert out.decision == ImageDecision.REVIEW and out.output is None
    assert out.review_blob is not None and "Image QC failed" in out.reason


async def test_image2_output_passing_qc_is_final(app, settings):
    case = _image_case(app)
    app.hub.cpa.returns["chat_json"] = qc(rendered_text="12 km")
    [out] = await _run_images(app, settings, *case)
    assert out.decision == ImageDecision.RECREATE and out.output is not None
    assert out.ai_generated and "QC passed" in out.reason


async def test_qc_error_counts_as_failure(app, settings):
    case = _image_case(app)
    app.hub.cpa.returns["chat_json"] = UpstreamError("cpa", 500, "boom")
    [out] = await _run_images(app, settings, *case)
    assert out.decision == ImageDecision.REVIEW and "QC unavailable" in out.reason


# --------------------------------------------------------------------------- 4. PyMySQL threads


class FakeConn:
    serial = itertools.count()

    def __init__(self, log: list[tuple[str, int, int]]) -> None:
        self.log = log
        self.n = next(FakeConn.serial)  # not id(): CPython reuses addresses of freed objects
        self.log.append(("connect", self.n, threading.get_ident()))

    def cursor(self) -> "FakeConn":
        self.log.append(("use", self.n, threading.get_ident()))
        return self

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, args: Any = None) -> int:
        return 1

    def fetchone(self) -> dict[str, int]:
        return {"ok": 1}

    def close(self) -> None:
        self.log.append(("close", self.n, threading.get_ident()))


async def test_each_db_operation_uses_its_own_connection_in_one_thread(monkeypatch, settings):
    log: list[tuple[str, int, int]] = []
    monkeypatch.setattr(db_pool.pymysql, "connect", lambda **_: FakeConn(log))
    db = db_pool.Database(settings.db, asyncio.Semaphore(3))
    await asyncio.gather(*(db.fetchone("SELECT 1") for _ in range(12)))

    conns: dict[int, list[tuple[str, int]]] = {}
    for action, conn_id, thread in log:
        conns.setdefault(conn_id, []).append((action, thread))
    assert len(conns) == 12  # one connection per operation, never reused
    for events in conns.values():
        assert [a for a, _ in events] == ["connect", "use", "close"]
        assert len({t for _, t in events}) == 1  # created, used and closed in the same thread
    assert threading.get_ident() not in {t for _, _, t in log}  # never on the event loop


# --------------------------------------------------------------------------- 5. forward grouping


def _event(msg: Any = None, messages: list[Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(sender_id=1, chat_id=5, message=msg, messages=messages,
                           respond=Recorder().respond)


async def test_regular_forwards_become_separate_tasks_and_albums_one(app):
    app.intake = Recorder().intake
    calls: list[list[Any]] = []

    async def intake(chat_id: int, user_id: int, messages: list[Any]) -> None:
        calls.append(messages)

    app.intake = intake
    bot = _bot(app)
    await bot._on_message(_event("fwd-1"))
    await bot._on_message(_event("fwd-2"))  # same second, unrelated -> separate task
    await bot._on_album(_event(messages=["a1", "a2", "a3"]))
    assert calls == [["fwd-1"], ["fwd-2"], ["a1", "a2", "a3"]]


def test_merge_window_collector_is_gone():
    import importlib.util

    assert importlib.util.find_spec("tg_x_copilot.pipeline.collector") is None
    assert "merge_window_seconds" not in AppSettings(_env_file=None).pipeline.model_dump()


# --------------------------------------------------------------------------- 6. env-only keys


def test_no_api_key_is_runtime_editable():
    assert not [k for k in EDITABLE_KEYS if is_secret_key(k)]
    for key in ("cpa.api_key", "jev.api_key", "r2.access_key_id", "r2.secret_access_key"):
        assert is_secret_key(key) and key not in EDITABLE_KEYS


async def test_config_service_rejects_and_ignores_keys_in_db(settings):
    repo = Recorder(get_settings={"jev.api_key": "leaked-from-db", "models.text_model": "m2"})
    svc = ConfigService(settings, repo)
    await svc.load()
    assert svc.current.jev.api_key.get_secret_value() == ""  # env value, DB row ignored
    assert svc.current.models.text_model == "m2"
    with pytest.raises(ValueError, match="env-only"):
        await svc.update({"cpa.api_key": "sk-new"})
    assert repo.named("set_settings") == []


# --------------------------------------------------------------------------- 7. atomic claims


async def test_claim_is_a_conditional_update():
    db = Recorder(execute=1)
    assert await Repository(db).claim_task("t1", "w1")
    sql, args = db.named("execute")[0][0]
    assert "WHERE id=%s AND status=%s" in sql
    assert args[-1] == TaskStatus.RECEIVED.value
    db.returns["execute"] = 0
    assert not await Repository(db).claim_task("t1", "w2")


class ClaimRepo(Recorder):
    """In-memory task table with the same compare-and-set semantics as the SQL claim."""

    def __init__(self) -> None:
        super().__init__()
        self.status = "received"
        self.owner: str | None = None

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return {"id": task_id, "status": self.status, "attempts": 0}

    async def claim_task(self, task_id: str, owner: str) -> bool:
        await asyncio.sleep(0)  # interleave competing workers
        if self.status != "received":
            return False
        self.status, self.owner = "processing", owner
        return True

    async def release_claim(self, task_id: str, owner: str) -> bool:
        if self.status == "processing" and self.owner == owner:
            self.status, self.owner = "received", None
            return True
        return False


async def test_two_workers_never_process_the_same_task(app):
    repo = ClaimRepo()
    ran: list[str] = []
    pipelines = []
    for owner in ("proc-a", "proc-b", "proc-c"):
        p = Pipeline(SimpleNamespace(**{**vars(app), "repo": repo, "instance_id": owner}))

        async def body(task_id: str, task: dict[str, Any], _o: str = owner) -> None:
            ran.append(_o)

        p._process_claimed = body  # type: ignore[method-assign]
        pipelines.append(p)
    await asyncio.gather(*(p.process("t1") for p in pipelines))
    assert len(ran) == 1


@pytest.mark.parametrize("shutting_down,expected", [(True, "received"), (False, "processing")])
async def test_claim_released_on_shutdown_but_not_on_timeout(app, shutting_down, expected):
    """Shutdown hands the task back; a per-task timeout keeps the claim so the worker can mark
    it FAILED without the sweeper re-running it in between."""
    repo = ClaimRepo()
    p = Pipeline(SimpleNamespace(**{**vars(app), "repo": repo, "shutting_down": shutting_down}))

    async def hang(task_id: str, task: dict[str, Any]) -> None:
        await asyncio.sleep(10)

    p._process_claimed = hang  # type: ignore[method-assign]
    job = asyncio.create_task(p.process("t1"))
    await asyncio.sleep(0.05)
    assert repo.status == "processing"
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert repo.status == expected


# --------------------------------------------------------------------------- 8. retention / review


def _ops(app, status: str, problems: list[str] | None = None) -> Ops:
    app.repo.returns["get_task"] = {"id": "t1", "status": status,
                                    "draft_meta": {"problems": problems or [], "review": ["x"]}}
    app.storage.returns["release_task"] = 0
    return Ops(app)


def test_storage_defaults_save_r2():
    s = AppSettings(_env_file=None).storage
    assert s.persist_review_media is False and s.retain_approved_assets is False


@pytest.mark.parametrize("retain,kinds", [(False, ("final", "review")), (True, ("review",))])
async def test_approve_releases_final_assets_unless_retained(app, retain, kinds):
    app.config.current.storage.retain_approved_assets = retain
    ok, _ = await _ops(app, "draft_ready").approve("t1")
    assert ok
    assert app.storage.named("release_task")[0][1]["kinds"] == kinds


async def test_needs_review_can_be_approved_only_without_blocking_problems(app):
    ok, _ = await _ops(app, "needs_review").approve("t1")
    assert ok
    ok, msg = await _ops(app, "needs_review", problems=["fabricated number"]).approve("t1")
    assert not ok and "blocking" in msg


def test_limits_include_all_workloads(settings):
    lim = Limits.from_settings(settings.concurrency)
    assert {"jev", "text", "vision", "image", "db", "io"} <= set(vars(lim))
