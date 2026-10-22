"""Issue #13: compare-and-set task transitions and CSRF protection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from conftest import Recorder
from tg_x_copilot.db.repo import Repository
from tg_x_copilot.image_settings import ImagePreferences
from tg_x_copilot.models import TaskStatus
from tg_x_copilot.pipeline.processor import Pipeline
from tg_x_copilot.services.ops import Ops
from tg_x_copilot.web.security import is_same_origin, require_same_origin


class CasRepo(Recorder):
    """One task row with the same compare-and-set semantics as the SQL."""

    def __init__(self, status: str, *, stale_read: str | None = None) -> None:
        super().__init__()
        self.status = status
        self.stale_read = stale_read  # what a racing reader saw before another device acted
        self.owner: str | None = None

    async def get_task(self, task_id: str) -> dict[str, Any]:
        seen, self.stale_read = (self.stale_read or self.status), None
        return {"id": task_id, "status": seen, "draft_meta": {}, "envelope": {"media": []},
                "tg_user_id": 1, "locale": "en-US"}

    def _cas(self, new: str, expected: Any) -> bool:
        if expected is not None and self.status not in {TaskStatus(e).value for e in expected}:
            return False
        self.status = new
        return True

    async def set_status(self, task_id: str, status: TaskStatus, *, stage: Any = None,
                         error: Any = None, expected: Any = None) -> bool:
        self.calls.append(("set_status", (status,), {"expected": expected}))
        return self._cas(status.value, expected)

    async def save_draft(self, task_id: str, status: TaskStatus, text: str, meta: Any, *,
                         expected: Any = None) -> bool:
        self.calls.append(("save_draft", (status,), {"expected": expected}))
        return self._cas(status.value, expected)

    async def reserve_task(self, task_id: str, owner: str, expected: Any) -> bool:
        if self._cas("processing", expected):
            self.owner = owner
            return True
        return False

    async def release_claim(self, task_id: str, owner: str) -> bool:
        if self.status == "processing" and self.owner == owner:
            self.status, self.owner = "received", None
            return True
        return False


def ops_for(app: SimpleNamespace, repo: CasRepo) -> Ops:
    app.repo = repo
    app.storage.returns["release_task"] = 0
    app.workers = Recorder(enqueue=True)
    app.image_preferences = Recorder(get=ImagePreferences())
    return Ops(app)


# --------------------------------------------------------------------------- repository SQL


async def test_set_status_with_expected_is_conditional_update():
    db = Recorder(execute=1)
    assert await Repository(db).set_status(
        "t1", TaskStatus.APPROVED, stage="done",
        expected=(TaskStatus.DRAFT_READY, TaskStatus.NEEDS_REVIEW))
    sql, args = db.named("execute")[0][0]
    assert sql.endswith("WHERE id=%s AND status IN (%s,%s)")
    assert args[-2:] == ["draft_ready", "needs_review"]
    db.returns["execute"] = 0
    assert not await Repository(db).save_draft("t1", TaskStatus.DRAFT_READY, "x", {},
                                               expected=(TaskStatus.PROCESSING,))


async def test_unconditional_set_status_still_supported():
    db = Recorder(execute=1)
    await Repository(db).set_status("t1", TaskStatus.RECEIVED)
    sql, _ = db.named("execute")[0][0]
    assert "status IN" not in sql


# --------------------------------------------------------------------------- operator actions


@pytest.mark.parametrize("action", ["approve", "publish", "reject", "not_publish"])
async def test_stale_operator_action_cannot_overwrite_newer_status(app, action):
    # The operator's page showed draft_ready, but another device already re-queued the
    # task and a worker claimed it.
    repo = CasRepo("processing", stale_read="draft_ready")
    ok, msg = await getattr(ops_for(app, repo), action)("t1")
    assert not ok and "changed" in msg.lower()
    assert repo.status == "processing"
    assert app.storage.named("release_task") == []  # nothing released for a lost race


async def test_regenerate_reserves_task_so_no_worker_sees_it_half_reset(app):
    repo = CasRepo("draft_ready")
    statuses_during_prep: list[str] = []

    async def release_task(*_: Any, **__: Any) -> int:
        statuses_during_prep.append(repo.status)
        return 0

    ops = ops_for(app, repo)
    app.storage.returns["release_task"] = release_task
    ok, _ = await ops.regenerate("t1")
    assert ok
    assert statuses_during_prep == ["processing"]  # reserved: not claimable while resetting
    assert repo.status == "received"  # handed to the queue afterwards
    assert app.workers.named("enqueue")


async def test_regenerate_loses_race_cleanly(app):
    repo = CasRepo("published", stale_read="draft_ready")  # status moved on meanwhile
    ok, msg = await ops_for(app, repo).regenerate("t1")
    assert not ok and "changed" in msg.lower()
    assert repo.status == "published"
    assert app.storage.named("release_task") == [] and app.workers.named("enqueue") == []


# --------------------------------------------------------------------------- pipeline writes


async def test_superseded_run_cannot_resurrect_rejected_task(app):
    repo = CasRepo("rejected")  # operator rejected while the worker was still running
    app.repo = repo
    saved = await Pipeline(app)._save_result("t1", TaskStatus.DRAFT_READY, "post", {})
    assert not saved and repo.status == "rejected"
    assert app.storage.named("release_task")  # assets persisted by the stale run are freed
    assert any(a[1] == "superseded" for a, _ in repo.named("add_event"))


async def test_superseded_run_keeps_assets_of_a_newer_run(app):
    repo = CasRepo("received")  # regenerated meanwhile: the new run owns the media rows
    app.repo = repo
    assert not await Pipeline(app)._save_result("t1", TaskStatus.DRAFT_READY, "post", {})
    assert app.storage.named("release_task") == []


async def test_pipeline_skip_and_fail_only_apply_while_processing(app):
    repo = CasRepo("rejected")
    app.repo = repo
    p = Pipeline(app)
    from tg_x_copilot.models import InputEnvelope
    await p._skip("t1", InputEnvelope(chat_id=1, user_id=1, message_ids=[1]), "low value")
    await p.mark_failed("t1", RuntimeError("boom"))
    assert repo.status == "rejected"


# --------------------------------------------------------------------------- CSRF


@pytest.mark.parametrize("headers,ok", [
    ({"host": "127.0.0.1:8080", "origin": "http://127.0.0.1:8080"}, True),
    ({"host": "127.0.0.1:8080", "origin": "https://evil.example"}, False),
    ({"host": "127.0.0.1:8080", "referer": "http://127.0.0.1:8080/tasks/x"}, True),
    ({"host": "127.0.0.1:8080", "referer": "https://evil.example/x"}, False),
    ({"host": "127.0.0.1:8080", "origin": "null"}, False),
    ({"host": "127.0.0.1:8080"}, True),  # curl / scripts: explicit credentials only
    ({"host": "10.0.0.2:8080", "x-forwarded-host": "copilot.example.com",
      "origin": "https://copilot.example.com"}, True),
])
def test_same_origin_decision(headers, ok):
    assert is_same_origin("POST", headers, []) is ok


def test_trusted_origin_and_safe_methods():
    cross = {"host": "127.0.0.1:8080", "origin": "https://copilot.example.com"}
    assert is_same_origin("POST", cross, ["https://copilot.example.com"])
    assert is_same_origin("GET", {"host": "a", "origin": "https://evil.example"}, [])


def test_cross_site_post_is_rejected_end_to_end(settings):
    app = FastAPI()
    app.state.ctx = SimpleNamespace(config=SimpleNamespace(base=settings))

    @app.post("/tasks/{task_id}/reject", dependencies=[Depends(require_same_origin)])
    async def reject(task_id: str) -> dict[str, str]:
        return {"rejected": task_id}

    client = TestClient(app)
    assert client.post("/tasks/t1/reject", headers={"Origin": "https://evil.example"}
                       ).status_code == 403
    assert client.post("/tasks/t1/reject", headers={"Origin": "http://testserver"}
                       ).status_code == 200
