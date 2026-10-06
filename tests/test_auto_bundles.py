"""Automatic creation, opt-in burst grouping and direct publishing-bundle controls."""

import asyncio
from types import SimpleNamespace

import pytest

from conftest import Recorder, png_bytes, sent_files
from test_bilingual import SOURCE, POST, bot, env, good_qc
from test_image_tools import PreferenceRepo, button_data
from test_image_delivery import MediaRepo, MemoryR2
from tg_x_copilot.app_context import AppContext
from tg_x_copilot.bot.bundles import BundleCollector
from tg_x_copilot.bot.image_tools import payload
from tg_x_copilot.image_settings import (
    ImageAction,
    ImageOption,
    ImageOptions,
    ImagePreferences,
    WorkflowMode,
    resolve_workflow,
)
from tg_x_copilot.i18n import t
from tg_x_copilot.models import (
    ImageAnalysis,
    ImageQC,
    SourceMedia,
    MediaKind,
    VerifiedFacts,
    VerifiedFact,
    RewriteResult,
    Evaluation,
    TriageResult,
)
from tg_x_copilot.pipeline.processor import Pipeline, LoadedImage
from tg_x_copilot.pipeline.media import inspect_image
from tg_x_copilot.services.image_preferences import ImagePreferenceService
from tg_x_copilot.services.ops import Ops
from tg_x_copilot.services.storage import AssetStore

TASK = "a" * 32


def preferences():
    return ImagePreferences(
        workflow_mode=WorkflowMode.AUTO_BUNDLE,
        image_action=ImageAction.ENHANCE,
        image_options=ImageOptions(
            flags={ImageOption.MINIMAL_CHANGES, ImageOption.REMOVE_OVERLAYS},
            promotion_targets={"0.promo"},
        ),
    )


def msg(idx, text="text", photo=False):
    return SimpleNamespace(
        id=idx,
        raw_text=text,
        photo=object() if photo else None,
        document=None,
        grouped_id=None,
        fwd_from=None,
        file=None,
    )


@pytest.mark.parametrize("images", [False, True])
def test_auto_mode_derives_creation_without_changing_manual_preferences_or_granting_rights(images):
    stored = preferences()
    before = stored.model_dump(mode="json")
    effective = resolve_workflow(stored, has_images=images)
    assert effective.image_action == (ImageAction.RECREATE if images else ImageAction.GENERATE)
    assert ImageOption.MINIMAL_CHANGES not in effective.image_options.flags
    assert ImageOption.REDESIGN in effective.image_options.flags
    assert not effective.image_options.promotion_targets
    assert stored.model_dump(mode="json") == before
    manual = stored.model_copy(update={"workflow_mode": WorkflowMode.MANUAL})
    assert resolve_workflow(manual, has_images=images) == manual


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
@pytest.mark.parametrize("images", [False, True])
async def test_intake_snapshots_auto_mode_queues_once_and_has_no_extra_status_message(
    app, code, images
):
    app.config.current.default_locale = code
    app.image_preferences = Recorder(get=preferences())
    app.workers = Recorder(enqueue=False)  # Durable task/sweeper covers a full queue.
    app.telegram = Recorder()
    app.repo.returns["create_task"] = TASK
    await AppContext.intake(app, 5, 1, [msg(1, SOURCE, images)])
    snapshot = app.repo.named("create_task")[0][0][0]
    assert snapshot.workflow_mode is WorkflowMode.AUTO_BUNDLE
    assert snapshot.image_action == (ImageAction.RECREATE if images else ImageAction.GENERATE)
    assert app.workers.named("enqueue") == [((TASK,), {"wait": False})]
    assert not app.telegram.named("notify")
    assert not snapshot.image_edit_authorizations
    assert not app.config.current.pipeline.direct_uploads_owned


async def test_collector_groups_text_album_and_more_text_sorted_and_deduplicated():
    collector = BundleCollector(idle_seconds=0.03, max_age_seconds=0.1)
    emitted = []

    async def emit(messages, prefs):
        emitted.append(([m.id for m in messages], prefs))

    p = preferences()
    await collector.add((5, 1), [msg(3)], p, emit)
    await collector.add((5, 1), [msg(1), msg(2), msg(3)], p, emit)
    p.workflow_mode = (
        WorkflowMode.MANUAL
    )  # Snapshot belongs to the group, not later global changes.
    await asyncio.sleep(0.06)
    assert len(emitted) == 1 and emitted[0][0] == [1, 2, 3]
    assert emitted[0][1].workflow_mode is WorkflowMode.AUTO_BUNDLE
    await collector.close()
    assert len(emitted) == 1


async def test_collector_isolates_user_chat_caps_bursts_and_flushes_before_shutdown():
    collector = BundleCollector(idle_seconds=10, max_messages=2)
    emitted = []

    async def emit(messages, prefs):
        emitted.append([m.id for m in messages])

    await collector.add((5, 1), [msg(1)], preferences(), emit)
    await collector.add((5, 2), [msg(2)], preferences(), emit)
    await collector.add((6, 1), [msg(3)], preferences(), emit)
    await collector.add((5, 1), [msg(4)], preferences(), emit)
    assert emitted == [[1, 4]]
    await collector.close()
    assert sorted(emitted) == [[1, 4], [2], [3]] and not collector.pending


async def test_collector_max_age_flushes_continuous_input():
    collector = BundleCollector(idle_seconds=0.2, max_age_seconds=0.05)
    emitted = []

    async def emit(messages, prefs):
        emitted.append([m.id for m in messages])

    await collector.add((5, 1), [msg(1)], preferences(), emit)
    await asyncio.sleep(0.02)
    await collector.add((5, 1), [msg(2)], preferences(), emit)
    await asyncio.sleep(0.05)
    assert emitted == [[1, 2]]
    await collector.close()


async def test_bot_auto_handlers_combine_album_and_text_and_shutdown_persists_snapshot(app):
    app.image_preferences = Recorder(get=preferences())
    calls = []

    async def intake(chat, user, messages, **kw):
        calls.append((chat, user, [m.id for m in messages], kw))

    app.intake = intake
    b = bot(app)
    r = Recorder()
    await b._on_message(SimpleNamespace(sender_id=1, chat_id=5, message=msg(1), respond=r.respond))
    await b._on_album(
        SimpleNamespace(
            sender_id=1,
            chat_id=5,
            messages=[msg(2, photo=True), msg(3, photo=True)],
            respond=r.respond,
        )
    )
    assert not calls
    await b.flush_intake()
    assert len(calls) == 1 and calls[0][2] == [1, 2, 3]
    assert calls[0][3]["preferences"].workflow_mode is WorkflowMode.AUTO_BUNDLE
    app.shutting_down = True
    await b._on_message(SimpleNamespace(sender_id=1, chat_id=5, message=msg(4), respond=r.respond))
    assert len(calls) == 2  # No volatile buffer after shutdown has begun.


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_workflow_toggle_callback_remembers_mode_and_keeps_manual_choices(app, code):
    app.config.current.default_locale = code
    repo = PreferenceRepo()
    app.repo = repo
    app.image_preferences = ImagePreferenceService(repo)
    app.ops = Recorder()
    await app.image_preferences.update(
        1, lambda p: preferences().model_copy(update={"workflow_mode": WorkflowMode.MANUAL})
    )
    b = bot(app)
    r = Recorder()
    event = SimpleNamespace(
        sender_id=1,
        chat_id=5,
        data=payload("-", "w", "auto_bundle"),
        answer=r.answer,
        respond=r.respond,
        edit=r.edit,
    )
    await b._on_callback(event)
    saved = await ImagePreferenceService(repo).get(1)
    assert (
        saved.workflow_mode is WorkflowMode.AUTO_BUNDLE
        and saved.image_action is ImageAction.ENHANCE
    )
    assert ImageOption.MINIMAL_CHANGES in saved.image_options.flags
    assert t(code, "image_tools_auto_bundle_help") in r.named("edit")[0][0][0]
    assert all(
        len(button_data(button)) <= 64 for row in r.named("edit")[0][1]["buttons"] for button in row
    )


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
@pytest.mark.parametrize("count", [0, 1, 2])
async def test_auto_creation_processes_every_source_without_edit_authorization_and_returns_bundle(
    app, code, count
):
    app.config.current.default_locale = code
    app.repo = MediaRepo(code)
    app.hub.r2 = MemoryR2()
    app.storage = AssetStore(app)
    effective = resolve_workflow(preferences(), has_images=count > 0)
    sources = [
        SourceMedia(message_id=10 + i, kind=MediaKind.PHOTO, forwarded=True, source_chat_id=-1009)
        for i in range(count)
    ]
    envelope = env(
        code,
        workflow_mode=WorkflowMode.AUTO_BUNDLE,
        image_action=effective.image_action,
        image_options=effective.image_options,
        media=sources,
    )
    images = [
        LoadedImage(i, inspect_image(png_bytes()), source) for i, source in enumerate(sources)
    ]
    analyses = {
        i: ImageAnalysis(
            description="Screenshot",
            image_type="ui_screenshot",
            relevance=0.9,
            has_channel_overlay=True,
            extracted_text=SOURCE,
        )
        for i in range(count)
    }
    app.hub.cpa.returns.update(
        images_generate=png_bytes("blue"), chat_json=good_qc(rendered_text="12")
    )
    packet = VerifiedFacts(facts=[VerifiedFact(text=SOURCE, evidence=SOURCE)])
    p = Pipeline(app)
    outcomes = await p._process_images(
        TASK,
        envelope,
        images,
        analyses,
        RewriteResult(post=POST[code], hook="Compatibility"),
        Evaluation(suitable=True, value_score=0.9),
        app.i18n.get(code),
        app.config.current,
        verified_facts=packet,
    )
    assert len(outcomes) == max(1, count) and all(o.output for o in outcomes)
    assert len(app.hub.cpa.named("images_generate")) == max(1, count)
    assert not app.hub.cpa.named("images_edit") and not envelope.image_edit_authorizations
    await p._ensure_media_rows(TASK, envelope, images, outcomes, analyses)
    results = await p._persist(TASK, images, outcomes, analyses, app.config.current, locale=code)
    app.repo.task.update(
        locale=code, draft_text=POST[code], envelope=envelope.model_dump(mode="json")
    )
    app.repo.task["draft_meta"]["media"] = [r.model_dump(mode="json") for r in results]
    b = bot(app)
    await b.send_draft(TASK)
    sent, kw = b.client.named("send_file")[0]
    assert len(sent_files(sent[1])) == max(1, count)
    assert (kw["caption"][0] if isinstance(kw["caption"], list) else kw["caption"]) == POST[code]
    assert len(b.client.named("send_message")) == 1
    if count < 2:
        buttons = kw["buttons"]
        assert any(button_data(btn) == f"t:i:{TASK}".encode() for row in buttons for btn in row)
    else:
        assert "buttons" not in kw  # Albums use the associated review card.
    assert "ia:" not in str(b.client.named("send_message")[0][1]["buttons"])


@pytest.mark.parametrize("passes_second", [True, False])
async def test_auto_qc_retry_corrects_once_and_never_persists_an_invalid_result(app, passes_second):
    effective = resolve_workflow(preferences(), has_images=False)
    envelope = env(
        workflow_mode=WorkflowMode.AUTO_BUNDLE,
        image_action=effective.image_action,
        image_options=effective.image_options,
    )
    tries = []

    def qc(model, messages, schema, **kw):
        tries.append(schema)
        return good_qc(numbers_consistent=passes_second and len(tries) == 2, rendered_text="12")

    app.hub.cpa.returns.update(images_generate=png_bytes(), chat_json=qc)
    [out] = await Pipeline(app)._process_images(
        TASK,
        envelope,
        [],
        {},
        RewriteResult(post=POST["en-US"], hook="Compatibility"),
        Evaluation(suitable=True, value_score=0.9),
        app.i18n.get("en-US"),
        app.config.current,
        verified_facts=VerifiedFacts(facts=[VerifiedFact(text=SOURCE, evidence=SOURCE)]),
    )
    assert len(app.hub.cpa.named("images_generate")) == 2 and tries == [ImageQC, ImageQC]
    assert "prior original visual" in app.hub.cpa.named("images_generate")[1][0][1]
    assert bool(out.output) == passes_second
    if not passes_second:
        assert out.decision.value == "review" and out.failure_stage.value == "QC"


@pytest.mark.parametrize("invalid", ["user", "chat"])
async def test_bundle_redo_rejects_wrong_owner_or_chat(app, invalid):
    app.repo.returns["get_task"] = {
        "id": TASK,
        "tg_user_id": 1,
        "tg_chat_id": 5,
        "status": "draft_ready",
        "draft_text": SOURCE,
        "envelope": {"media": []},
    }
    app.image_preferences = Recorder(get=preferences())
    app.workers = Recorder(enqueue=True)
    ok, _ = await Ops(app).rerun_bundle_images(
        TASK, 2 if invalid == "user" else 1, 6 if invalid == "chat" else 5
    )
    assert not ok and not app.repo.named("queue_image_rerun")


async def test_bundle_redo_uses_auto_defaults_and_keeps_draft_as_an_image_only_job(app):
    app.repo.returns.update(
        get_task={
            "id": TASK,
            "tg_user_id": 1,
            "tg_chat_id": 5,
            "status": "draft_ready",
            "draft_text": SOURCE,
            "envelope": {"media": [{"kind": "photo"}]},
        },
        queue_image_rerun=True,
    )
    app.image_preferences = Recorder(get=preferences())
    app.workers = Recorder(enqueue=True)
    app.ops = Ops(app)
    b = bot(app)
    r = Recorder()
    event = SimpleNamespace(
        sender_id=1, chat_id=5, data=f"t:i:{TASK}".encode(), answer=r.answer, respond=r.respond
    )
    await b._on_callback(event)
    args, _ = app.repo.named("queue_image_rerun")[0]
    assert args[:2] == (TASK, 1) and args[2].workflow_mode is WorkflowMode.AUTO_BUNDLE
    assert args[2].image_action is ImageAction.RECREATE
    assert r.named("answer") and not app.hub.cpa.calls


async def test_auto_mode_keeps_jev_judgment_but_evaluates_requested_valid_content(app):
    auto = env(workflow_mode=WorkflowMode.AUTO_BUNDLE, image_action=ImageAction.GENERATE)
    app.repo.returns["get_task"] = {"envelope": auto.model_dump(mode="json"), "attempts": 0}
    p = Pipeline(app)
    triage = TriageResult(
        route="skip", value=0.1, use_media=False, reasons=["low value"], content_type="practical"
    )

    async def fake_triage(*a):
        return triage

    async def editorial(*a, **kw):
        reviewed = a[4]
        assert reviewed.route == "review" and "low value" in reviewed.reasons
        assert reviewed.escalated

    p._triage = fake_triage
    p._run_editorial = editorial
    from tg_x_copilot.pipeline.cleaning import CoreContent

    app.hub.cpa.returns["chat_json"] = CoreContent(core_text=SOURCE)
    await p._process_claimed(TASK, app.repo.returns["get_task"])
    assert not app.repo.named("set_status")


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
@pytest.mark.parametrize("images", [False, True])
async def test_forward_intake_to_complete_pipeline_needs_no_callbacks_or_authorization(
    app, code, images
):
    from tg_x_copilot.models import FactVerification, TaskStatus
    from tg_x_copilot.pipeline.cleaning import CoreContent
    from tg_x_copilot.pipeline.language import LanguageCheck

    class FullRepo(MediaRepo):
        def __init__(self):
            super().__init__(code)
            self.returns.update(get_rules={}, get_hooks=[], claim_task=True)

        async def create_task(self, envelope):
            self.task.update(
                envelope=envelope.model_dump(mode="json"),
                attempts=0,
                status="received",
                locale=code,
            )
            return TASK

        async def save_draft(self, task_id, status, text, meta):
            self.task.update(status=status.value, draft_text=text, draft_meta=meta)

    app.repo = FullRepo()
    app.hub.r2 = MemoryR2()
    app.storage = AssetStore(app)
    app.config.current.default_locale = code
    app.config.current.jev.enabled = False
    app.image_preferences = Recorder(get=preferences())
    app.workers = Recorder(enqueue=True)
    b = bot(app)
    app.telegram = b
    b.fetch_media = Recorder(fetch_media={1: png_bytes("red")}).fetch_media
    fact = "新功能支持 12 台设备。" if code == "zh-CN" else "The new feature supports 12 devices."

    def model(model, messages, schema, **kw):
        if schema is CoreContent:
            return CoreContent(core_text=SOURCE)
        if schema is ImageAnalysis:
            return ImageAnalysis(
                description="来源信息图" if code == "zh-CN" else "Source infographic",
                relevance=0.9,
                image_type="ui_screenshot",
                has_channel_overlay=True,
                extracted_text=SOURCE,
            )
        if schema is Evaluation:
            return Evaluation(
                suitable=True,
                value_score=0.9,
                angle="功能" if code == "zh-CN" else "Feature",
                key_facts=[fact],
            )
        if schema is RewriteResult:
            return RewriteResult(post=POST[code], hook="功能" if code == "zh-CN" else "Feature")
        if schema is VerifiedFacts:
            return VerifiedFacts(facts=[VerifiedFact(text=fact, evidence=SOURCE)])
        if schema is LanguageCheck:
            return LanguageCheck(passed=True)
        if schema is FactVerification:
            return FactVerification(passed=True)
        if schema is ImageQC:
            return good_qc(rendered_text="12")
        raise AssertionError(schema)

    app.hub.cpa.returns.update(chat_json=model, images_generate=png_bytes("blue"))
    await AppContext.intake(app, 5, 1, [msg(1, SOURCE, images)])
    assert not b.client.calls  # Quiet intake: only the final publishing bundle is returned.
    await Pipeline(app).process(TASK)
    assert b.client.named("send_file")[0][1]["caption"] == POST[code]
    assert all(entry.get("sent") for entry in app.repo.task["draft_meta"]["delivery"].values())
    assert not app.hub.cpa.named("images_edit")
    assert not app.repo.task["envelope"]["image_edit_authorizations"]
    assert app.repo.task["status"] in (TaskStatus.DRAFT_READY.value, TaskStatus.NEEDS_REVIEW.value)
    assert [name for name, _, _ in b.client.calls] == ["send_file", "send_message"]


async def test_auto_bundle_respects_x_image_count_without_requiring_review_of_extra_sources(app):
    effective = resolve_workflow(preferences(), has_images=True)
    sources = [SourceMedia(message_id=idx + 1, kind=MediaKind.PHOTO) for idx in range(6)]
    envelope = env(
        workflow_mode=WorkflowMode.AUTO_BUNDLE,
        image_action=effective.image_action,
        image_options=effective.image_options,
        media=sources,
    )
    images = [
        LoadedImage(idx, inspect_image(png_bytes()), source) for idx, source in enumerate(sources)
    ]
    analyses = {
        idx: ImageAnalysis(
            description="Source", image_type="ui_screenshot", relevance=0.9, extracted_text=SOURCE
        )
        for idx in range(6)
    }
    app.hub.cpa.returns.update(images_generate=png_bytes(), chat_json=good_qc(rendered_text="12"))
    results = await Pipeline(app)._process_images(
        TASK,
        envelope,
        images[:4],
        analyses,
        RewriteResult(post=POST["en-US"], hook="Feature"),
        Evaluation(suitable=True, value_score=0.9),
        app.i18n.get("en-US"),
        app.config.current,
        verified_facts=VerifiedFacts(facts=[VerifiedFact(text=SOURCE, evidence=SOURCE)]),
        max_images=4,
    )
    assert len([out for out in results if out.output]) == 4
    assert [out.decision.value for out in results[4:]] == ["omit", "omit"]
    assert not any(out.decision.value == "review" for out in results)


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_photo_only_without_ocr_uses_visible_observations_as_image_evidence(app, code):
    from tg_x_copilot.models import FactVerification
    from tg_x_copilot.pipeline.facts import build_verified_facts, packet_is_traceable
    from tg_x_copilot.pipeline.language import LanguageCheck
    from tg_x_copilot.pipeline.image_content import editorial_evidence

    observation = (
        "照片中可见绿色树木。" if code == "zh-CN" else "Green trees are visible in the image."
    )
    a = ImageAnalysis(
        description=observation,
        image_type="photo_generic",
        extracted_text="",
        source_facts=[observation],
    )
    envelope = env(code, workflow_mode=WorkflowMode.AUTO_BUNDLE)
    envelope.text = ""
    packet = VerifiedFacts(
        facts=[VerifiedFact(text=observation, evidence=observation, source_idx=0)]
    )

    def respond(model, messages, schema, **kw):
        if schema is VerifiedFacts:
            return packet
        if schema is LanguageCheck:
            return LanguageCheck(passed=True)
        return FactVerification(passed=True)

    app.hub.cpa.returns["chat_json"] = respond
    assert (await build_verified_facts(envelope, {0: a}, app)).facts == packet.facts
    assert packet_is_traceable(packet, "", {0: a})
    assert a.extracted_text == "" and editorial_evidence(a) == observation
    a.sensitive = True
    assert not packet_is_traceable(packet, "", {0: a}) and editorial_evidence(a) == ""


async def test_explicit_tools_rerun_can_override_one_task_while_auto_mode_stays_enabled(app):
    from test_image_tools import ready_task, TASK

    repo = PreferenceRepo()
    repo.returns["get_task"] = ready_task()
    app.repo = repo
    app.image_preferences = ImagePreferenceService(repo)
    app.ops = Recorder(rerun_images=(True, "queued"))
    await app.image_preferences.update(
        1, lambda p: preferences().model_copy(update={"image_action": ImageAction.INFO_CARD})
    )
    b = bot(app)
    r = Recorder()
    event = SimpleNamespace(
        sender_id=1,
        chat_id=5,
        data=payload(TASK, "run"),
        answer=r.answer,
        respond=r.respond,
        edit=r.edit,
    )
    await b._on_callback(event)
    passed = app.ops.named("rerun_images")[0][0][2]
    assert (
        passed.image_action is ImageAction.INFO_CARD and passed.workflow_mode is WorkflowMode.MANUAL
    )
    assert (await app.image_preferences.get(1)).workflow_mode is WorkflowMode.AUTO_BUNDLE


async def test_shutdown_still_drains_workers_and_closes_services_if_buffer_flush_fails(app):
    app.workers = Recorder()
    app.telegram = Recorder(flush_intake=RuntimeError("intake unavailable"))
    app.db = Recorder()
    closer = Recorder()
    app.hub.close = closer.close
    app._sweeper = None
    await AppContext.stop(app)
    assert app.shutting_down and app.workers.named("stop")
    assert app.telegram.named("stop") and app.db.named("close") and closer.named("close")
