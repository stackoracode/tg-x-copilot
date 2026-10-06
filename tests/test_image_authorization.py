"""Task/image-scoped permission callbacks and durable retry through the real media pipeline."""

import copy
import json
from types import SimpleNamespace

import pytest

from conftest import Recorder, png_bytes, sent_files
from test_bilingual import bot, good_qc
from test_image_delivery import MediaRepo, setup, process, promotion
from test_image_tools import button_data
from tg_x_copilot.db.repo import Repository
from tg_x_copilot.i18n import t
from tg_x_copilot.models import ImageAnalysis, ImageQC, ImageEditAuthorization, InputEnvelope
from tg_x_copilot.pipeline.image_policy import has_task_edit_authorization
from tg_x_copilot.pipeline.language import LanguageCheck
from tg_x_copilot.pipeline.overlays import unchanged_outside
from tg_x_copilot.pipeline.processor import Pipeline
from tg_x_copilot.services.ops import Ops

TASK = "a" * 32


class AuthorizationRepo(MediaRepo):
    def __init__(self, code):
        super().__init__(code)
        self.returns["get_rules"] = {}
        self.authorizations = []
        self.upserts = []

    async def upsert_media(self, task_id, idx, **kw):
        self.upserts.append(idx)
        old = self.media.get(idx, {})
        self.media[idx] = {
            **old,
            "idx": idx,
            **kw,
            "analysis": None,
            "decision": None,
            "decision_reason": None,
            "ai_generated": False,
        }

    async def update_media_analysis(self, task_id, idx, analysis):
        self.media[idx]["analysis"] = analysis.model_dump(mode="json")

    async def clear_media_asset(self, task_id, idx):
        self.media[idx].update(asset_key=None, asset_kind=None)

    async def save_draft(self, task_id, status, text, meta):
        self.task.update(status=status.value, draft_text=text, draft_meta=meta)

    async def authorize_image_edit(self, task_id, user_id, chat_id, authorization, reason):
        if self.task["status"] not in ("draft_ready", "needs_review"):
            return False
        self.authorizations.append(authorization)
        envelope = self.task["envelope"]
        envelope.setdefault("image_edit_authorizations", {})[str(authorization.image_idx)] = (
            authorization.model_dump(mode="json")
        )
        envelope.update(
            processing_mode="images_only", image_retry_indices=[authorization.image_idx]
        )
        self.task["status"] = "received"
        return True


async def blocked_task(app, code="en-US", other_image=False):
    envelope, analysis, image = setup(app, code, cleanup=True, rights=False)
    analysis.description = "原始截图" if code == "zh-CN" else "Original screenshot"
    app.repo = AuthorizationRepo(code)
    if other_image:
        source = image.source.model_copy(update={"message_id": 11})
        envelope.media.append(source)
    app.repo.task["envelope"] = envelope.model_dump(mode="json")
    app.repo.task["draft_meta"].update(
        canonical_text=envelope.text,
        text_review=[],
        source_analyses={"0": analysis.model_dump(mode="json")},
    )
    await process(app, envelope, analysis, image)
    app.repo.task["status"] = "needs_review"
    if other_image:
        app.repo.media[1] = dict(
            idx=1,
            decision="keep",
            asset_kind="final",
            asset_key="other.png",
            source_sha256="b" * 64,
            decision_reason="Unchanged",
        )
        app.hub.r2.objects["other.png"] = png_bytes("green")
        meta = app.repo.task["draft_meta"]
        meta["media"] = [m for m in meta["media"] if m["idx"] != 1] + [
            dict(
                idx=1,
                decision="keep",
                asset_kind="final",
                asset_key="other.png",
                failure_stage=None,
            )
        ]
        meta["delivery"] = {"1": {"sent": True, "message_id": 10}}
    b = bot(app)
    app.telegram = b
    app.workers = Recorder(enqueue=True)
    app.ops = Ops(app)
    fetch = Recorder(fetch_media={10: image.blob.data})
    b.fetch_media = fetch.fetch_media

    def vision(model, messages, schema, **kw):
        if schema is ImageAnalysis:
            return analysis
        if schema is LanguageCheck:
            return LanguageCheck(passed=True)
        if schema is ImageQC:
            return good_qc(
                protected_marks_preserved=True,
                promotion_removal_valid=True,
                outside_regions_unchanged=True,
            )
        raise AssertionError(
            "Authorization retry must not call an editorial LLM: " + schema.__name__
        )

    app.hub.cpa.returns["chat_json"] = vision
    return b, envelope, analysis, image, fetch


async def click(b, user=1, chat=5, data=f"ia:{TASK}:0"):
    r = Recorder()
    event = SimpleNamespace(
        sender_id=user,
        chat_id=chat,
        data=data.encode(),
        answer=r.answer,
        respond=r.respond,
        edit=r.edit,
    )
    await b._on_callback(event)
    return r


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_card_callback_authorizes_only_current_image_and_completes_delivery(app, code):
    b, envelope, analysis, image, fetch = await blocked_task(app, code, other_image=True)
    draft = app.repo.task["draft_text"]
    other = copy.deepcopy(app.repo.media[1])
    await b.send_draft(TASK)
    buttons = b.client.named("send_message")[0][1]["buttons"]
    matching = [
        button
        for row in buttons
        for button in row
        if button_data(button) == f"ia:{TASK}:0".encode()
    ]
    assert len(matching) == 1 and matching[0].text == t(code, "btn_confirm_edit_rights", idx=0)
    prior_other_receipt = copy.deepcopy(app.repo.task["draft_meta"]["delivery"]["1"])
    b.client.calls.clear()
    event = await click(b)
    assert event.named("answer") and event.named("respond")[0][0][0] == t(code, "edit_auth_queued")
    persisted = InputEnvelope.model_validate(app.repo.task["envelope"])
    assert persisted.image_retry_indices == [0]
    assert persisted.image_edit_authorizations["0"].source_sha256 == image.blob.sha256
    assert not app.config.current.pipeline.direct_uploads_owned
    assert app.workers.named("enqueue") == [((TASK,), {"wait": False})]
    app.repo.upserts.clear()
    await Pipeline(app)._process_images_only(
        TASK, app.repo.task, persisted, app.i18n.get(code), app.config.current
    )
    assert set(app.repo.upserts) == {0}
    assert fetch.named("fetch_media")[0][0] == (5, [10])
    assert app.repo.media[1] == other and app.hub.r2.objects["other.png"] == png_bytes("green")
    assert app.repo.task["draft_text"] == draft
    final = app.hub.r2.objects[app.repo.media[0]["asset_key"]]
    assert app.repo.media[0]["asset_kind"] == "final"
    assert unchanged_outside(image.blob.data, final, [promotion()])
    assert [name for name, _, _ in b.client.calls] == ["send_file", "send_message"]
    photo = sent_files(b.client.named("send_file")[0][0][1])
    assert len(photo) == 1 and photo[0].getvalue() == final
    assert app.repo.task["draft_meta"]["delivery"]["0"]["sent"] is True
    assert app.repo.task["draft_meta"]["delivery"]["1"] == prior_other_receipt
    assert len(app.hub.cpa.named("images_edit")) == 1
    assert not app.hub.jev.calls
    assert all(
        call[0][2] in (ImageAnalysis, LanguageCheck, ImageQC)
        for call in app.hub.cpa.named("chat_json")
    )
    assert not app.repo.named("delete_media_rows") and not app.repo.named("save_image_preferences")


@pytest.mark.parametrize(
    "failure,display_stage",
    [
        ("image2", "IMAGE2"),
        ("qc", "IMAGE_QC"),
        ("copyright", "IMAGE_QC"),
        ("upload", "R2_UPLOAD"),
        ("fetch", "R2_FETCH"),
        ("send", "TELEGRAM_SEND"),
    ],
)
@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_authorized_retry_failure_stage_is_visible_and_no_failed_final_image(
    app, failure, display_stage, code
):
    b, envelope, analysis, image, fetch = await blocked_task(app, code)
    await click(b)
    if failure == "image2":
        app.hub.cpa.returns["images_edit"] = ConnectionError("not shown secret URL")
    if failure in ("qc", "copyright"):
        original = app.hub.cpa.returns["chat_json"]

        def failed(model, messages, schema, **kw):
            if schema is ImageQC:
                return good_qc(
                    protected_marks_preserved=failure != "copyright",
                    promotion_removal_valid=True,
                    outside_regions_unchanged=True,
                    numbers_consistent=failure != "qc",
                    watermarks_ok=failure != "copyright",
                )
            return original(model, messages, schema, **kw)

        app.hub.cpa.returns["chat_json"] = failed
    app.hub.r2.upload_fail = failure == "upload"
    app.hub.r2.fetch_fail = failure == "fetch"
    if failure == "send":
        b.client.returns["send_file"] = ConnectionError("not shown secret URL")
    persisted = InputEnvelope.model_validate(app.repo.task["envelope"])
    await Pipeline(app)._process_images_only(
        TASK, app.repo.task, persisted, app.i18n.get(code), app.config.current
    )
    card = b.client.named("send_message")[-2][0][1]
    assert "[" + display_stage + "]" in card
    assert "not shown secret URL" not in card
    assert app.repo.task["status"] == "needs_review"
    assert not app.repo.task["draft_meta"]["delivery"]["0"]["sent"]
    assert b.client.named("send_message")[-1][0][1] == app.repo.task["draft_text"]
    if failure in ("image2", "qc", "copyright", "upload"):
        assert not app.repo.media[0].get("asset_key") and not b.client.named("send_file")


@pytest.mark.parametrize("invalid", ["user", "chat", "idx", "stale", "repeated", "malformed"])
async def test_callback_rejects_unauthorized_or_stale_confirmation(app, invalid):
    b, *_ = await blocked_task(app)
    if invalid == "stale":
        app.repo.task["status"] = "approved"
    if invalid == "repeated":
        await click(b)
    r = await click(
        b,
        user=2 if invalid == "user" else 1,
        chat=6 if invalid == "chat" else 5,
        data=f"ia:{TASK}:7"
        if invalid == "idx"
        else "ia:invalid:0"
        if invalid == "malformed"
        else f"ia:{TASK}:0",
    )
    assert r.named("answer")
    assert len(app.repo.authorizations) == (1 if invalid == "repeated" else 0)
    assert len(app.workers.named("enqueue")) == (1 if invalid == "repeated" else 0)


@pytest.mark.parametrize("change", ["task", "image", "hash", "user"])
def test_explicit_permission_is_bound_to_task_image_source_and_actor(change):
    from test_bilingual import env

    authorization = ImageEditAuthorization(
        task_id=TASK, image_idx=0, user_id=1, source_sha256="b" * 64
    )
    envelope = env(image_edit_authorizations={"0": authorization})
    assert has_task_edit_authorization(envelope, task_id=TASK, idx=0, source_sha256="b" * 64)
    if change == "user":
        envelope.user_id = 2
    assert not has_task_edit_authorization(
        envelope,
        task_id="c" * 32 if change == "task" else TASK,
        idx=1 if change == "image" else 0,
        source_sha256="c" * 64 if change == "hash" else "b" * 64,
    )


async def test_changed_source_hash_is_not_editable_after_confirmation(app):
    b, envelope, analysis, image, fetch = await blocked_task(app)
    await click(b)
    fetch.returns["fetch_media"] = {10: png_bytes("green", (100, 100))}
    persisted = InputEnvelope.model_validate(app.repo.task["envelope"])
    await Pipeline(app)._process_images_only(
        TASK, app.repo.task, persisted, app.i18n.get("en-US"), app.config.current
    )
    assert not app.hub.cpa.named("images_edit")
    assert not b.client.named("send_file")
    assert "[IMAGE_POLICY]" in b.client.named("send_message")[-2][0][1]


async def test_permission_does_not_allow_protected_region_selection(app):
    b, envelope, analysis, image, fetch = await blocked_task(app)
    # User-selected target is attribution, not promotion: permission never overrides this.
    app.repo.task["envelope"]["image_options"]["promotion_targets"] = ["0.credit"]
    await click(b)
    persisted = InputEnvelope.model_validate(app.repo.task["envelope"])
    await Pipeline(app)._process_images_only(
        TASK, app.repo.task, persisted, app.i18n.get("en-US"), app.config.current
    )
    assert not app.hub.cpa.named("images_edit") and not b.client.named("send_file")
    assert "[IMAGE_POLICY]" in b.client.named("send_message")[-2][0][1]


async def test_queue_sql_is_atomic_and_does_not_write_global_preferences():
    auth = ImageEditAuthorization(task_id=TASK, image_idx=2, user_id=1, source_sha256="b" * 64)
    db = Recorder(execute=1)
    assert await Repository(db).authorize_image_edit(TASK, 1, 5, auth, "[IMAGE_POLICY] reason")
    sql, args = db.named("execute")[0][0]
    assert "JOIN task_media" in sql and "m.source_sha256=%s" in sql
    assert "t.tg_chat_id=%s" in sql and "t.tg_user_id=%s" in sql and "t.status IN (%s,%s)" in sql
    assert "settings" not in sql and "draft_text=" not in sql
    assert "$.image_retry_indices" in sql and "$.image_edit_authorizations" in sql
    assert json.loads(args[2]) == [2]
    assert json.loads(args[4])["source_sha256"] == "b" * 64
    db.returns["execute"] = 0
    assert not await Repository(db).authorize_image_edit(TASK, 1, 5, auth, "[IMAGE_POLICY] reason")


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_localized_permission_labels_are_real_catalog_entries(app, code):
    assert t(code, "btn_confirm_edit_rights", idx=0) != "btn_confirm_edit_rights"
    assert t(code, "edit_auth_queued") != "edit_auth_queued"
    assert t(code, "error_connection") != "error_connection"


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_existing_review_card_image_tools_exposes_permission_without_llm_or_rerun(app, code):
    from tg_x_copilot.services.image_preferences import ImagePreferenceService

    b, *_ = await blocked_task(app, code)
    app.image_preferences = ImagePreferenceService(app.repo)
    r = await click(b, data=f"it:{TASK}:v:main")
    buttons = r.named("edit")[0][1]["buttons"]
    assert any(button_data(button) == f"ia:{TASK}:0".encode() for row in buttons for button in row)
    assert not app.hub.cpa.calls and not app.workers.named("enqueue")


async def test_explicitly_selected_promotion_rectangle_leaves_other_promotion_and_credits_intact(
    app,
):
    b, envelope, analysis, image, fetch = await blocked_task(app)
    untouched = promotion().model_copy(update={"id": "other", "box": (0.7, 0.1, 1, 0.2)})
    analysis.mark_regions.append(untouched)
    app.repo.task["envelope"]["image_options"]["promotion_targets"] = ["0.tg"]
    await click(b)
    persisted = InputEnvelope.model_validate(app.repo.task["envelope"])
    await Pipeline(app)._process_images_only(
        TASK, app.repo.task, persisted, app.i18n.get("en-US"), app.config.current
    )
    final = app.hub.r2.objects[app.repo.media[0]["asset_key"]]
    assert len(app.hub.cpa.named("images_edit")) == 1
    assert unchanged_outside(image.blob.data, final, [promotion()])
