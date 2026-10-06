"""Real processing/store/delivery boundaries, with isolated R2 and Telegram transports."""

from types import SimpleNamespace

import pytest

from conftest import Recorder, png_bytes
from test_bilingual import SOURCE, bot, env, good_qc
from tg_x_copilot.image_settings import ImageAction, ImageOption, ImageOptions
from tg_x_copilot.models import (
    ImageAnalysis,
    MarkRegion,
    MediaKind,
    RewriteResult,
    Evaluation,
    SourceMedia,
    VerifiedFacts,
    VerifiedFact,
)
from tg_x_copilot.pipeline.media import inspect_image
from tg_x_copilot.pipeline.overlays import (
    approved_regions,
    composite_patches,
    unchanged_outside,
    validate_pixel_scope,
)
from tg_x_copilot.pipeline.image_qc import qc_verdict
from tg_x_copilot.pipeline.processor import LoadedImage, Pipeline
from tg_x_copilot.services.storage import AssetStore, AssetAttachmentError


class MediaRepo(Recorder):
    def __init__(self, code):
        super().__init__(stored_bytes=0)
        self.media = {}
        self.task = dict(
            id="a" * 32,
            tg_chat_id=5,
            tg_user_id=1,
            locale=code,
            status="draft_ready",
            score=0.9,
            draft_text="Draft" if code == "en-US" else "草稿",
            draft_meta={},
        )

    async def upsert_media(self, task_id, idx, **kw):
        self.media[idx] = dict(idx=idx, **kw)

    async def set_media_asset(self, task_id, idx, **kw):
        if idx not in self.media:
            return False
        self.media[idx].update(asset_key=kw["key"], asset_kind=kw["kind"])
        return True

    async def asset_key_referenced(self, key):
        return any(m.get("asset_key") == key for m in self.media.values())

    async def update_media_decision(self, task_id, idx, decision, reason, **kw):
        self.media[idx].update(decision=decision, decision_reason=reason, **kw)

    async def list_media(self, task_id):
        return list(self.media.values())

    async def get_task(self, task_id):
        return self.task

    async def record_media_delivery(self, task_id, delivery, status):
        self.task["draft_meta"]["delivery"] = delivery
        self.task["status"] = status.value


class MemoryR2(Recorder):
    def __init__(self):
        super().__init__()
        self.objects = {}
        self.upload_fail = self.fetch_fail = False

    async def exists(self, key):
        return key in self.objects

    async def put_object(self, key, data, mime):
        if self.upload_fail:
            raise ConnectionError("upload unavailable")
        self.objects[key] = data

    async def get_object(self, key):
        if self.fetch_fail:
            raise ConnectionError("fetch unavailable")
        return self.objects[key]

    async def delete_object(self, key):
        self.objects.pop(key, None)


def promotion():
    return MarkRegion(
        id="tg",
        text="TG channel",
        kind="promotion",
        box=(0.1, 0.8, 0.9, 1),
        confidence=0.99,
        safe_to_remove=True,
    )


def copyright():
    return MarkRegion(
        id="credit",
        text="© Photographer",
        kind="photographer",
        box=(0, 0, 0.2, 0.1),
        confidence=0.99,
    )


def setup(app, code="en-US", cleanup=False, rights=True):
    app.repo = MediaRepo(code)
    app.hub.r2 = MemoryR2()
    app.storage = AssetStore(app)
    app.config.current.pipeline.direct_uploads_owned = rights
    source = SourceMedia(message_id=10, kind=MediaKind.PHOTO, forwarded=False)
    options = (
        ImageOptions(flags={ImageOption.MINIMAL_CHANGES, ImageOption.REMOVE_OVERLAYS})
        if cleanup
        else ImageOptions()
    )
    envelope = env(code, media=[source], image_action=ImageAction.ENHANCE, image_options=options)
    analysis = ImageAnalysis(
        description="Original",
        image_type="ui_screenshot",
        relevance=0.9,
        extracted_text=SOURCE,
        contains_text=True,
        text_language="en",
        text_script="latin",
        has_source_copyright_mark=cleanup,
        has_channel_overlay=cleanup,
        has_third_party_watermark=cleanup,
        mark_regions=[promotion(), copyright()] if cleanup else [],
    )
    # Unmarked tests localize source text to the user locale; cleanup deliberately preserves
    # original English UI even for zh-CN, as explicitly required by minimal-edit semantics.
    if code == "zh-CN" and not cleanup:
        analysis.contains_text = False
    image = LoadedImage(0, inspect_image(png_bytes("red", (100, 100))), source)
    app.hub.cpa.returns.update(
        images_edit=png_bytes("blue", (100, 100)),
        chat_json=good_qc(
            protected_marks_preserved=True,
            promotion_removal_valid=True,
            outside_regions_unchanged=True,
        ),
    )
    return envelope, analysis, image


async def process(app, envelope, analysis, image):
    p = Pipeline(app)
    outcomes = await p._process_images(
        "a" * 32,
        envelope,
        [image],
        {0: analysis},
        RewriteResult(post=SOURCE, hook="12 devices", image_brief="Compatibility"),
        Evaluation(suitable=True, value_score=0.9),
        app.i18n.get(envelope.locale),
        app.config.current,
        verified_facts=VerifiedFacts(facts=[VerifiedFact(text=SOURCE, evidence=SOURCE)]),
    )
    await p._ensure_media_rows("a" * 32, envelope, [image], outcomes, {0: analysis})
    results = await p._persist(
        "a" * 32, [image], outcomes, {0: analysis}, app.config.current, locale=envelope.locale
    )
    app.repo.task["draft_meta"]["media"] = [r.model_dump(mode="json") for r in results]
    return outcomes[0], results[0]


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
@pytest.mark.parametrize("cleanup", [False, True])
async def test_final_asset_reaches_telegram_as_photo_before_card_and_draft(app, code, cleanup):
    envelope, analysis, image = setup(app, code, cleanup)
    outcome, result = await process(app, envelope, analysis, image)
    assert outcome.output and result.asset_kind == "final" and not result.failure_stage
    final = app.hub.r2.objects[result.asset_key]
    if cleanup:
        assert unchanged_outside(image.blob.data, final, [promotion()])
        assert inspect_image(final).width == image.blob.width
        assert app.hub.cpa.named("images_edit")[0][0][2] != image.blob.data  # local crop only
    b = bot(app)
    await b.send_draft("a" * 32)
    assert [n for n, _, _ in b.client.calls] == ["send_file", "send_message", "send_message"]
    args, kwargs = b.client.named("send_file")[0]
    assert args[1][0].getvalue() == final and args[1][0].name.endswith(".png")
    assert kwargs["force_document"] is False
    assert app.repo.task["draft_meta"]["delivery"]["0"]["sent"] is True
    assert b.client.named("send_message")[-1][0][1] == app.repo.task["draft_text"]


@pytest.mark.parametrize(
    "failure,stage",
    [
        ("rights", "IMAGE_POLICY"),
        ("image2", "IMAGE2"),
        ("qc", "QC"),
        ("copyright", "QC"),
        ("upload", "R2_UPLOAD"),
        ("fetch", "R2_FETCH"),
        ("send", "TELEGRAM_SEND"),
        ("document", "TELEGRAM_SEND"),
        ("invalid_bytes", "R2_FETCH"),
    ],
)
@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_failures_have_specific_review_stage_and_still_deliver_draft(
    app, failure, stage, code
):
    envelope, analysis, image = setup(app, code, cleanup=True, rights=failure != "rights")
    if failure == "image2":
        app.hub.cpa.returns["images_edit"] = ConnectionError("Image2 unavailable")
    if failure in ("qc", "copyright"):
        app.hub.cpa.returns["chat_json"] = good_qc(
            numbers_consistent=failure != "qc",
            watermarks_ok=failure != "copyright",
            protected_marks_preserved=failure != "copyright",
            promotion_removal_valid=True,
            outside_regions_unchanged=True,
        )
    app.hub.r2.upload_fail = failure == "upload"
    outcome, result = await process(app, envelope, analysis, image)
    if failure in ("qc", "copyright"):
        assert outcome.review_blob is not None and not result.asset_key
        assert not app.hub.r2.objects
    app.hub.r2.fetch_fail = failure == "fetch"
    if failure == "invalid_bytes":
        app.hub.r2.objects[result.asset_key] = b"not an image"
    b = bot(app)
    if failure == "send":
        b.client.returns["send_file"] = ConnectionError("Telegram unavailable")
    if failure == "document":
        b.client.returns["send_file"] = [SimpleNamespace(photo=None, document=object())]
    await b.send_draft("a" * 32)
    receipt = app.repo.task["draft_meta"]["delivery"]["0"]
    assert receipt["failure_stage"] == stage and not receipt["sent"]
    assert app.repo.task["status"] == "needs_review"
    card = b.client.named("send_message")[-2][0][1]
    display_stage = "IMAGE_QC" if stage == "QC" else stage
    assert f"[{display_stage}]" in card and "❌" in card
    assert b.client.named("send_message")[-1][0][1] == app.repo.task["draft_text"]
    if stage not in ("TELEGRAM_SEND",):
        assert not b.client.named("send_file")


async def test_delivery_retry_clears_transport_failure_without_regenerating_draft(app):
    envelope, analysis, image = setup(app)
    await process(app, envelope, analysis, image)
    b = bot(app)
    b.client.returns["send_file"] = ConnectionError("Telegram down")
    await b.send_draft("a" * 32)
    b.client.returns["send_file"] = [SimpleNamespace(photo=object(), id=123)]
    await b.send_draft("a" * 32)
    assert app.repo.task["draft_meta"]["delivery"]["0"] == {"sent": True, "message_id": 123}
    assert app.repo.task["draft_text"] == "Draft"
    assert app.repo.task["status"] == "draft_ready"
    assert len(app.hub.cpa.named("images_edit")) == 1


@pytest.mark.parametrize("kind", ["author", "photographer", "copyright", "media_rights", "unknown"])
def test_protected_marks_are_never_selected_or_overwritten(kind):
    mark = copyright().model_copy(update={"kind": kind})
    analysis = ImageAnalysis(
        description="Original",
        relevance=0.9,
        has_source_copyright_mark=True,
        mark_regions=[promotion(), mark],
    )
    with pytest.raises(ValueError):
        approved_regions(analysis, ImageOptions(promotion_targets={"0.credit"}), 0)
    unsafe = promotion().model_copy(update={"box": mark.box})
    analysis.mark_regions = [unsafe, mark]
    with pytest.raises(ValueError):
        approved_regions(analysis, ImageOptions(), 0)


@pytest.mark.parametrize(
    "field",
    [
        "watermarks_ok",
        "protected_marks_preserved",
        "promotion_removal_valid",
        "outside_regions_unchanged",
    ],
)
def test_cleanup_qc_fails_closed_on_each_safety_gate(field):
    qc = good_qc(
        protected_marks_preserved=True, promotion_removal_valid=True, outside_regions_unchanged=True
    ).model_copy(update={field: False})
    assert not qc_verdict(qc, allowed_texts=[SOURCE], mode="promotion_cleanup")[0]


def test_pixel_check_catches_outside_changes_and_protects_rounded_credit_regions():
    region = promotion()
    source = png_bytes("red", (100, 100))
    composed = composite_patches(source, [region], [png_bytes("blue")])
    assert unchanged_outside(source, composed.data, [region])
    assert not unchanged_outside(source, png_bytes("blue", (100, 100)), [region])
    analysis = ImageAnalysis(
        description="Original",
        mark_regions=[
            region.model_copy(update={"box": (0.1, 0.801, 0.9, 1)}),
            copyright().model_copy(update={"box": (0.1, 0.79, 0.9, 0.8001)}),
        ],
    )
    with pytest.raises(ValueError):
        validate_pixel_scope(source, [analysis.mark_regions[0]], analysis)


async def test_asset_without_media_pointer_fails_and_removes_orphan(app):
    setup(app)
    with pytest.raises(AssetAttachmentError):
        await app.storage.persist("missing", 0, inspect_image(png_bytes()), "final")
    assert not app.hub.r2.objects


async def test_stale_final_pointer_never_delivers_qc_rejected_image(app):
    envelope, analysis, image = setup(app)
    _, result = await process(app, envelope, analysis, image)
    app.repo.media[0].update(decision="review", decision_reason="[QC] Rejected")
    app.repo.task["draft_meta"]["media"][0]["failure_stage"] = "QC"
    b = bot(app)
    await b.send_draft("a" * 32)
    assert not b.client.named("send_file")
    assert app.repo.task["draft_meta"]["delivery"]["0"]["failure_stage"] == "QC"
    assert result.asset_key in app.hub.r2.objects


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_minimal_and_remove_callbacks_combine_and_remember(app, code):
    from test_image_tools import PreferenceRepo, ready_task, TASK
    from tg_x_copilot.bot.image_tools import payload
    from tg_x_copilot.services.image_preferences import ImagePreferenceService

    repo = PreferenceRepo()
    repo.returns["get_task"] = ready_task(code)
    app.repo = repo
    app.image_preferences = ImagePreferenceService(repo)
    app.ops = Recorder()
    app.config.current.default_locale = code
    b = bot(app)
    for value in ("minimal_changes,1", "remove_overlays,1"):
        r = Recorder()
        event = SimpleNamespace(
            sender_id=1,
            chat_id=5,
            data=payload(TASK, "o", value),
            answer=r.answer,
            respond=r.respond,
            edit=r.edit,
        )
        await b._on_callback(event)
        assert r.named("edit")
    prefs = await ImagePreferenceService(repo).get(1)
    assert {ImageOption.MINIMAL_CHANGES, ImageOption.REMOVE_OVERLAYS} <= prefs.image_options.flags


async def test_repository_attachment_distinguishes_idempotence_from_missing_row():
    from tg_x_copilot.db.repo import Repository

    db = Recorder(execute=0, fetchone={"asset_key": "k", "asset_kind": "final"})
    repo = Repository(db)
    assert await repo.set_media_asset("t", 0, key="k", kind="final", size=1, mime="image/png")
    db.returns["fetchone"] = None
    assert not await repo.set_media_asset("t", 0, key="k", kind="final", size=1, mime="image/png")


async def test_delivery_receipt_update_does_not_change_draft_or_terminal_tasks():
    from tg_x_copilot.db.repo import Repository
    from tg_x_copilot.models import TaskStatus

    db = Recorder()
    await Repository(db).record_media_delivery(
        "t", {"0": {"sent": False, "failure_stage": "QC"}}, TaskStatus.NEEDS_REVIEW
    )
    sql, args = db.named("execute")[0][0]
    assert "draft_text" not in sql and "status IN (%s,%s)" in sql
    assert args[-2:] == ("draft_ready", "needs_review")


@pytest.mark.parametrize("tamper", ["permission", "regions", "outside"])
async def test_cleanup_verification_rejects_invalid_contract_or_outside_pixels_before_vision(
    app, tamper
):
    import json

    envelope, analysis, image = setup(app, cleanup=True)
    regions = [promotion()]
    candidate = composite_patches(image.blob.data, regions, [png_bytes("blue")])
    contract = {
        "editing_rights_confirmed": tamper != "permission",
        "explicit_promotion_removal": True,
        "selected_regions": []
        if tamper == "regions"
        else [r.model_dump(mode="json") for r in regions],
    }
    if tamper == "outside":
        candidate = inspect_image(png_bytes("blue", (100, 100)))
    passed, _ = await Pipeline(app)._qc(
        envelope,
        app.i18n.get("en-US"),
        app.config.current,
        "promotion_cleanup",
        candidate,
        image,
        facts=SOURCE,
        post=SOURCE,
        cleanup_contract=json.dumps(contract),
        cleanup_regions=regions,
    )
    assert not passed and not app.hub.cpa.named("chat_json")


@pytest.mark.parametrize("ambiguous", ["legacy", "unlocated", "unsafe", "uncertain"])
def test_promotion_scope_fails_closed_when_analysis_is_ambiguous(ambiguous):
    a = ImageAnalysis(
        description="Original",
        has_source_copyright_mark=False,
        has_third_party_watermark=True,
        mark_regions=[promotion()],
    )
    if ambiguous == "legacy":
        a.has_source_copyright_mark = None
    if ambiguous == "unlocated":
        a.has_source_copyright_mark = True
    if ambiguous == "unsafe":
        a.mark_regions[0].safe_to_remove = False
    if ambiguous == "uncertain":
        a.mark_regions[0].confidence = 0.6
    with pytest.raises(ValueError):
        approved_regions(a, ImageOptions(), 0)
