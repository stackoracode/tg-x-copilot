"""Image intent, preference memory, transport callbacks and durable image-only jobs."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from conftest import Recorder, png_bytes
from test_bilingual import POST, SOURCE as BASE_SOURCE, bot, env as base_env, good_qc
from tg_x_copilot.bot.image_tools import keyboard, payload
from tg_x_copilot.db.repo import Repository
from tg_x_copilot.image_settings import (
    ACTIONS,
    PRESETS,
    ImageAction,
    ImageOption,
    ImageOptions,
    ImagePreferences,
    InformationDensity,
    MediaCategory,
)
from tg_x_copilot.i18n import t
from tg_x_copilot.models import (
    Evaluation,
    FactVerification,
    ImageAnalysis,
    ImageDecision,
    ImageQC,
    RewriteResult,
    SourceMedia,
    MediaKind,
    VerifiedFact,
    VerifiedFacts,
)
from tg_x_copilot.pipeline.cleaning import CoreContent
from tg_x_copilot.pipeline.facts import build_verified_facts
from tg_x_copilot.pipeline.image_policy import category, plan
from tg_x_copilot.pipeline.image_qc import build_messages, qc_verdict
from tg_x_copilot.pipeline.language import LanguageCheck
from tg_x_copilot.pipeline.media import inspect_image
from tg_x_copilot.pipeline.processor import LoadedImage, Pipeline
from tg_x_copilot.services.image_preferences import ImagePreferenceService
from tg_x_copilot.services.ops import Ops

SOURCE = BASE_SOURCE + " Acme X12 uses HTTP/3."
TASK = "a" * 32


def env(code="en-US", **kw):
    original = base_env(code, **kw)
    original.text = SOURCE
    return original


def button_data(button):
    # Telethon supports both old KeyboardButtonCallback and new InlineButtonTypeCallback.
    raw = button.to_dict()
    return raw.get("data", raw.get("type", {}).get("data", b""))


def packet(code="en-US"):
    return VerifiedFacts(
        facts=[
            VerifiedFact(
                text=SOURCE
                if code == "en-US"
                else "Acme X12 支持 12 台设备，使用 HTTP/3。",
                evidence=SOURCE,
            )
        ]
    )


class PreferenceRepo(Recorder):
    def __init__(self):
        super().__init__()
        self.preferences = {}

    async def get_image_preferences(self, user_id):
        return self.preferences.get(user_id)

    async def save_image_preferences(self, user_id, value):
        self.preferences[user_id] = value


def ready_task(code="en-US", action=ImageAction.GENERATE):
    envelope = env(code, image_action=action, processing_mode="images_only")
    return {
        "id": TASK,
        "status": "draft_ready",
        "attempts": 1,
        "tg_user_id": 1,
        "tg_chat_id": 5,
        "locale": code,
        "draft_text": POST[code],
        "envelope": envelope.model_dump(mode="json"),
        "draft_meta": {
            "verified_facts": packet(code).model_dump(mode="json"),
            "canonical_text": SOURCE,
            "source_analyses": {},
            "hook": "Compatibility" if code == "en-US" else "兼容性",
            "text_review": [],
            "text_warnings": [],
            "problems": [],
            "review": [],
        },
    }


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
@pytest.mark.parametrize("action", list(ImageAction))
def test_action_option_registry_and_mutual_exclusion(code, action):
    prefs = ImagePreferences(image_action=action)
    assert prefs.image_action == action and ACTIONS[action]
    with pytest.raises(ValidationError):
        ImagePreferences(image_action=[action, ImageAction.KEEP])
    buttons = keyboard(TASK, prefs, lambda key, **kw: t(code, key, **kw))
    selected = [
        b.text
        for row in buttons
        for b in row
        if button_data(b) == payload(TASK, "a", action.value)
    ]
    assert selected == ["☑ " + t(code, "image_action_" + action.value)]
    assert all(len(button_data(b)) <= 64 for row in buttons for b in row)


def test_combinable_preferences_resolve_conflicting_layout_choices():
    opts = ImageOptions().set_flag(ImageOption.SIMILAR_LAYOUT, True)
    opts = opts.set_flag(ImageOption.VISUAL_PRIORITY, True)
    opts = opts.set_flag(ImageOption.REDESIGN, True)
    assert (
        ImageOption.REDESIGN in opts.flags
        and ImageOption.SIMILAR_LAYOUT not in opts.flags
    )
    assert (
        ImageOption.VISUAL_PRIORITY in opts.flags
        and ImageOption.FACTUAL_PRIORITY in opts.flags
    )
    with pytest.raises(ValidationError):
        ImageOptions(flags=[ImageOption.MINIMAL_CHANGES, ImageOption.REDESIGN])
    with pytest.raises(ValidationError):
        ImageOptions(target_locale="xx-XX")
    with pytest.raises(ValidationError):
        ImagePreferences(api_key="not-a-preference")


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_persistent_preferences_survive_service_restart_and_user_isolation(code):
    repo = PreferenceRepo()
    service = ImagePreferenceService(repo)
    prefs = ImagePreferences(
        image_action=ImageAction.CLEAN_RECREATE,
        image_options=ImageOptions(
            flags=PRESETS[ImageAction.CLEAN_RECREATE],
            information_density="high",
            target_locale=code,
        ),
    )
    await service.update(1, lambda previous: prefs)
    restarted = ImagePreferenceService(repo)
    assert await restarted.get(1) == prefs
    assert await restarted.get(2) == ImagePreferences()
    assert (
        await restarted.get(2)
    ).image_options.information_density is InformationDensity.MEDIUM


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
@pytest.mark.parametrize("density", list(InformationDensity))
async def test_text_only_generation_uses_packet_angle_locale_and_density(
    app, code, density
):
    original = env(
        code,
        image_action=ImageAction.GENERATE,
        image_options=ImageOptions(information_density=density),
    )
    original.text += "\nNOISY CHANNEL RAW CONTENT SHOULD NEVER REACH GENERATOR"
    app.hub.cpa.returns.update(
        images_generate=png_bytes(), chat_json=good_qc(rendered_text="12")
    )
    [result] = await Pipeline(app)._process_images(
        TASK,
        original,
        [],
        {},
        RewriteResult(
            post=POST[code],
            hook="Compatibility matters" if code == "en-US" else "先确认兼容性",
        ),
        Evaluation(suitable=True, value_score=0.8),
        app.i18n.get(code),
        app.config.current,
        verified_facts=packet(code),
    )
    assert (
        result.decision is ImageDecision.GENERATE
        and result.output
        and result.ai_generated
    )
    prompt = app.hub.cpa.named("images_generate")[0][0][1]
    assert "NOISY CHANNEL" not in prompt and density.value in prompt
    assert "12" in prompt and (
        "简体中文" in prompt if code == "zh-CN" else "English" in prompt
    )
    if code == "zh-CN":
        assert "Acme X12" in prompt and "HTTP/3" in prompt
    assert (
        len(
            [
                part
                for part in app.hub.cpa.named("chat_json")[0][0][1][-1]["content"]
                if part["type"] == "image_url"
            ]
        )
        == 1
    )
    app.repo.returns["get_task"] = {
        "id": TASK,
        "tg_chat_id": 5,
        "locale": code,
        "status": "draft_ready",
        "draft_text": POST[code],
        "draft_meta": {},
        "score": 0.8,
    }
    app.repo.returns["list_media"] = [
        {
            "idx": 0,
            "decision": "generate",
            "asset_kind": "final",
            "asset_key": "a.png",
            "ai_generated": 1,
        }
    ]
    app.hub.r2.returns["get_object"] = result.output.data
    b = bot(app)
    await b.send_draft(TASK)
    assert (
        b.client.named("send_file")
        and b.client.named("send_message")[-1][0][1] == POST[code]
    )


async def test_explicit_image_locale_override_reaches_creation_and_qc(app):
    original = env(
        "en-US",
        image_action=ImageAction.INFO_CARD,
        image_options=ImageOptions(target_locale="zh-CN"),
    )
    app.hub.cpa.returns.update(images_generate=png_bytes(), chat_json=good_qc())
    [result] = await Pipeline(app)._process_images(
        TASK,
        original,
        [],
        {},
        RewriteResult(post=POST["en-US"], hook="Compatibility"),
        Evaluation(suitable=True, value_score=0.8),
        app.i18n.get("en-US"),
        app.config.current,
        verified_facts=packet(),
    )
    assert result.output and "Selected image action" in result.reason
    assert "简体中文" in app.hub.cpa.named("images_generate")[0][0][1]
    assert "简体中文" in app.hub.cpa.named("chat_json")[0][0][1][0]["content"]


@pytest.mark.parametrize("action", [ImageAction.OMIT, ImageAction.TEXT_ONLY])
async def test_omit_and_text_only_do_not_call_image_or_vision_services(app, action):
    original = env(
        image_action=action, media=[SourceMedia(message_id=10, kind=MediaKind.PHOTO)]
    )
    result = await Pipeline(app)._process_images(
        TASK,
        original,
        [],
        {},
        RewriteResult(post="same", hook="same"),
        Evaluation(suitable=True, value_score=0.8),
        app.i18n.get("en-US"),
        app.config.current,
    )
    assert [row.decision.value for row in result] == [action.value]
    assert not app.hub.cpa.calls


async def test_missing_packet_never_generates_unverified_image(app):
    [result] = await Pipeline(app)._process_images(
        TASK,
        env(image_action=ImageAction.GENERATE),
        [],
        {},
        RewriteResult(post="Invented 999", hook="Invented 999"),
        Evaluation(suitable=True, value_score=0.8),
        app.i18n.get("en-US"),
        app.config.current,
    )
    assert result.decision is ImageDecision.REVIEW and not result.output
    assert not app.hub.cpa.named("images_generate")


@pytest.mark.parametrize(
    "field",
    [
        "numbers_consistent",
        "dates_consistent",
        "names_consistent",
        "identifiers_consistent",
        "brands_consistent",
        "facts_consistent",
        "people_consistent",
        "language_consistent",
        "readability_ok",
        "density_consistent",
    ],
)
def test_any_failed_fact_language_or_quality_check_requires_review(field):
    assert not qc_verdict(good_qc(**{field: False}), allowed_texts=[SOURCE])[0]


def test_informational_vs_documentary_qc_rules_and_new_categories():
    for kind, expected in [
        ("photo_real_event", MediaCategory.DOCUMENTARY),
        ("ui_screenshot", MediaCategory.UI_SCREENSHOT),
        ("infographic", MediaCategory.INFOGRAPHIC),
        ("mixed_layout", MediaCategory.MIXED_LAYOUT),
        ("brand_asset", MediaCategory.BRAND_ASSET),
        (None, MediaCategory.GENERIC_VISUAL),
        ("text_input", MediaCategory.TEXT_INPUT),
    ]:
        assert category(kind) is expected
    photo = ImageAnalysis(
        description="news photo",
        image_type="photo_real_event",
        depicts_real_people=True,
        relevance=0.9,
    )
    for action in (
        ImageAction.RECREATE,
        ImageAction.CLEAN_RECREATE,
        ImageAction.LOCALIZE,
    ):
        resolved = plan(
            photo,
            requested=action,
            options=ImageOptions(),
            owned=False,
            target_language="en",
            locale="en-US",
        )
        assert (
            resolved.action is ImageAction.INFO_CARD and resolved.execution == "create"
        )
    assert (
        plan(
            photo,
            requested=ImageAction.ENHANCE,
            options=ImageOptions(),
            owned=True,
            target_language="en",
            locale="en-US",
        ).execution
        == "edit"
    )
    for action in (ImageAction.ENHANCE, ImageAction.KEEP):
        assert (
            plan(
                photo,
                requested=action,
                options=ImageOptions(),
                owned=False,
                target_language="en",
                locale="en-US",
            ).execution
            == "review"
        )
    shared = dict(
        locale="en-US",
        market="US",
        language_name="English (US)",
        facts=SOURCE,
        reference_text=SOURCE,
        candidate_url="candidate",
        reference_url="source",
    )
    recreate = build_messages("regenerate", **shared)[0]["content"]
    enhance = build_messages("enhance", **shared)[0]["content"]
    assert "not pixel similarity" in recreate and "exactly the same content" in enhance


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_image_only_job_never_calls_editor_or_changes_draft(app, code):
    task = ready_task(code)
    app.repo.returns.update(get_task=task, claim_task=True, get_rules={})
    app.hub.cpa.returns.update(
        images_generate=png_bytes(), chat_json=good_qc(rendered_text="12")
    )
    app.storage.returns["persist"] = "new.png"
    app.telegram = Recorder()
    await Pipeline(app).process(TASK)
    _, status, text, meta = app.repo.named("save_draft")[0][0]
    assert text == task["draft_text"] and status.value == "draft_ready"
    assert (
        meta["hook"] == task["draft_meta"]["hook"]
        and meta["verified_facts"] == task["draft_meta"]["verified_facts"]
    )
    assert all(args[2] is ImageQC for args, _ in app.hub.cpa.named("chat_json"))
    assert not app.hub.jev.calls and app.telegram.named("send_draft")
    assert app.repo.named("delete_media_rows") and app.storage.named("release_task")


async def test_image_only_timeout_keeps_draft_and_existing_assets(app):
    task = ready_task()
    app.repo.returns["get_task"] = task
    app.telegram = Recorder()
    await Pipeline(app).mark_failed(TASK, TimeoutError("image timeout"))
    _, status, text, meta = app.repo.named("save_draft")[0][0]
    assert text == task["draft_text"] and status.value == "needs_review"
    assert t("en-US", "image_rerun_failed") in meta["review"]
    assert not app.storage.named("release_task")


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_image_tools_callbacks_memory_presets_options_density_locale_and_queue(
    app, code
):
    app.config.current.default_locale = code
    repo = PreferenceRepo()
    repo.returns.update(get_task=ready_task(code), queue_image_rerun=True)
    app.repo = repo
    app.image_preferences = ImagePreferenceService(repo)
    app.workers = Recorder(enqueue=True)
    app.ops = Ops(app)
    b = bot(app)
    events = []

    async def click(command, value=""):
        recorder = Recorder()
        event = SimpleNamespace(
            sender_id=1,
            chat_id=5,
            data=payload(TASK, command, value),
            answer=recorder.answer,
            respond=recorder.respond,
            edit=recorder.edit,
        )
        await b._on_callback(event)
        assert recorder.named("answer")
        events.append(recorder)
        return recorder

    for command, value in [
        ("v", "main"),
        ("p", "clean_recreate"),
        ("o", "similar_layout,1"),
        ("o", "similar_layout,1"),
        ("d", "high"),
        ("l", code),
    ]:
        await click(command, value)
    prefs = await app.image_preferences.get(1)
    assert prefs.image_action is ImageAction.CLEAN_RECREATE
    assert ImageOption.SIMILAR_LAYOUT in prefs.image_options.flags
    assert (
        ImageOption.REDESIGN not in prefs.image_options.flags
    )  # explicit option resolves conflict
    assert prefs.image_options.information_density is InformationDensity.HIGH
    assert prefs.image_options.target_locale == code
    await click("run")
    assert repo.named("queue_image_rerun")[0][0] == (TASK, 1, prefs)
    assert app.workers.named("enqueue")[0][0] == (TASK,)
    assert events[-1].named("respond")[0][0][0] == t(code, "image_tools_queued")
    assert await ImagePreferenceService(repo).get(1) == prefs


async def test_callbacks_reject_foreign_tasks_and_malformed_values(app):
    repo = PreferenceRepo()
    repo.returns["get_task"] = {**ready_task(), "tg_user_id": 2}
    app.repo = repo
    app.image_preferences = ImagePreferenceService(repo)
    app.ops = Ops(app)
    b = bot(app)
    for data in (
        payload(TASK, "a", "generate"),
        b"it:invalid:a:generate",
        b"it:-:a:unknown",
        b"it:-:o:redesign,invalid",
    ):
        r = Recorder()
        event = SimpleNamespace(
            sender_id=1,
            chat_id=5,
            data=data,
            answer=r.answer,
            respond=r.respond,
            edit=r.edit,
        )
        await b._on_callback(event)
        assert r.named("respond")
    assert repo.preferences == {} and not repo.named("queue_image_rerun")


async def test_busy_task_image_callback_does_not_replace_or_enqueue(app):
    app.repo.returns["get_task"] = {**ready_task(), "status": "processing"}
    app.workers = Recorder(enqueue=True)
    ok, _ = await Ops(app).rerun_images(
        TASK, 1, ImagePreferences(image_action="generate")
    )
    assert not ok and not app.repo.named("queue_image_rerun") and not app.workers.calls


async def test_repository_image_job_uses_owner_and_status_cas_and_separate_preference_namespace():
    db = Recorder(execute=1, fetchall=[])
    repo = Repository(db)
    assert await repo.queue_image_rerun(
        TASK, 1, ImagePreferences(image_action="generate")
    )
    sql, args = db.named("execute")[0][0]
    assert (
        "tg_user_id=%s" in sql and "status IN (%s,%s)" in sql and "images_only" in sql
    )
    assert "draft_text=" not in sql and "draft_meta=" not in sql
    assert args[-2:] == ("draft_ready", "needs_review")
    await repo.get_settings()
    assert "NOT LIKE 'image_preferences:%'" in db.named("fetchall")[0][0][0]
    with pytest.raises(ValidationError):
        await repo.save_image_preferences(1, {"password": "not-allowed"})
    assert len(db.named("execute")) == 1


@pytest.mark.parametrize(
    "bad",
    [
        VerifiedFact(text="Acme supports 999 devices.", evidence=SOURCE),
        VerifiedFact(text=SOURCE, evidence="not in the source"),
    ],
)
async def test_fact_packet_requires_exact_evidence_and_no_invented_numbers(app, bad):
    app.hub.cpa.returns["chat_json"] = VerifiedFacts(facts=[bad])
    with pytest.raises(ValueError):
        await build_verified_facts(env(), {}, app)


async def test_fact_packet_semantic_verification_rejects_changed_meaning(app):
    def chat(model, messages, schema, **kwargs):
        if schema is VerifiedFacts:
            return packet()
        if schema is LanguageCheck:
            return LanguageCheck(passed=True)
        return FactVerification(passed=False)

    app.hub.cpa.returns["chat_json"] = chat
    with pytest.raises(ValueError, match="not entailed"):
        await build_verified_facts(env(), {}, app)


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_full_text_only_generation_pipeline_builds_facts_then_delivers_bundle(
    app, code
):
    original = env(code, image_action=ImageAction.GENERATE)
    app.repo.returns.update(
        get_task={
            "id": TASK,
            "status": "received",
            "attempts": 0,
            "envelope": original.model_dump(mode="json"),
        },
        claim_task=True,
        get_rules={},
        get_hooks=[],
    )
    app.config.current.jev.enabled = False
    app.telegram = Recorder()

    def chat(model, messages, schema, **kwargs):
        if schema is CoreContent:
            return CoreContent(core_text=SOURCE)
        if schema is LanguageCheck:
            return LanguageCheck(passed=True)
        if schema is VerifiedFacts:
            return packet(code)
        if schema is FactVerification:
            return FactVerification(passed=True)
        if schema is Evaluation:
            return Evaluation(suitable=True, value_score=0.8)
        if schema is RewriteResult:
            return RewriteResult(
                post=POST[code],
                hook="兼容性" if code == "zh-CN" else "Compatibility",
                added_value="核对设备适配。"
                if code == "zh-CN"
                else "Check device compatibility.",
            )
        if schema is ImageQC:
            return good_qc(rendered_text="12")
        raise AssertionError(schema)

    app.hub.cpa.returns.update(chat_json=chat, images_generate=png_bytes())
    app.storage.returns["persist"] = "generated.png"
    await Pipeline(app).process(TASK)
    _, status, text, meta = app.repo.named("save_draft")[0][0]
    assert (
        status.value == "draft_ready"
        and text == POST[code]
        and meta["verified_facts"]["facts"]
    )
    assert app.repo.named("upsert_media")[0][1]["kind"] == "generated"
    assert app.storage.named("persist") and app.telegram.named("send_draft")


async def test_intake_snapshots_remembered_image_preferences(app):
    from tg_x_copilot.app_context import AppContext

    app.repo = PreferenceRepo()
    app.repo.returns["create_task"] = TASK
    app.image_preferences = ImagePreferenceService(app.repo)
    prefs = ImagePreferences(
        image_action="generate",
        image_options=ImageOptions(information_density="high", target_locale="zh-CN"),
    )
    await app.image_preferences.update(1, lambda previous: prefs)
    app.workers = Recorder(enqueue=True)
    message = SimpleNamespace(
        id=10,
        raw_text=SOURCE,
        photo=None,
        document=None,
        fwd_from=None,
        grouped_id=None,
    )
    await AppContext.intake(app, 5, 1, [message])
    intake = app.repo.named("create_task")[0][0][0]
    assert (
        intake.image_action is ImageAction.GENERATE
        and intake.image_options == prefs.image_options
    )
    await app.image_preferences.update(
        1, lambda previous: ImagePreferences(image_action="omit")
    )
    assert intake.image_action is ImageAction.GENERATE


async def test_persisted_preference_schema_and_json_round_trip():
    import json

    db = Recorder(execute=1)
    repo = Repository(db)
    prefs = ImagePreferences(
        image_action="generate",
        image_options=ImageOptions(information_density="low", target_locale="zh-CN"),
    )
    await repo.save_image_preferences(1, prefs.model_dump(mode="json"))
    _, args = db.named("execute")[0][0]
    assert args[0] == "image_preferences:1"
    db.returns["fetchone"] = {"v": json.dumps(prefs.model_dump(mode="json"))}
    assert ImagePreferences.model_validate(await repo.get_image_preferences(1)) == prefs


async def test_source_information_recreation_uses_confirmed_facts_and_layout_preference(
    app,
):
    analysis = ImageAnalysis(
        description="UI screenshot",
        image_type="ui_screenshot",
        relevance=0.9,
        extracted_text=SOURCE,
        layout_description="Title above two cards",
        has_channel_overlay=True,
    )
    image = LoadedImage(
        0,
        inspect_image(png_bytes()),
        SourceMedia(
            message_id=10, kind=MediaKind.PHOTO, forwarded=True, source_chat_id=-1001
        ),
    )
    original = env(
        image_action=ImageAction.CLEAN_RECREATE,
        media=[image.source],
        image_options=ImageOptions().set_flag(ImageOption.SIMILAR_LAYOUT, True),
    )
    app.hub.cpa.returns.update(
        images_generate=png_bytes(), chat_json=good_qc(rendered_text="12")
    )
    [result] = await Pipeline(app)._process_images(
        TASK,
        original,
        [image],
        {0: analysis},
        RewriteResult(post=POST["en-US"], hook="Compatibility"),
        Evaluation(suitable=True, value_score=0.8),
        app.i18n.get("en-US"),
        app.config.current,
        verified_facts=packet(),
    )
    assert result.decision is ImageDecision.CLEAN_RECREATE and result.output
    prompt = app.hub.cpa.named("images_generate")[0][0][1]
    assert (
        "Title above two cards" in prompt
        and "Do not erase or patch watermarks" in prompt
    )
    assert not app.hub.cpa.named("images_edit")


async def test_tampered_packet_evidence_blocks_execution(app):
    invalid = VerifiedFacts(
        facts=[VerifiedFact(text="Supports 99 devices.", evidence="not a source quote")]
    )
    [result] = await Pipeline(app)._process_images(
        TASK,
        env(image_action="generate"),
        [],
        {},
        RewriteResult(post="99", hook="99"),
        Evaluation(suitable=True, value_score=0.8),
        app.i18n.get("en-US"),
        app.config.current,
        verified_facts=invalid,
    )
    assert result.decision is ImageDecision.REVIEW and not app.hub.cpa.named(
        "images_generate"
    )


@pytest.mark.parametrize("page", ["main", "options", "presets", "density", "locale"])
@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
def test_every_submenu_callback_is_short_and_localized(page, code):
    prefs = ImagePreferences(
        image_action="generate", image_options=ImageOptions(target_locale=code)
    )
    rows = keyboard(TASK, prefs, lambda key, **kw: t(code, key, **kw), page)
    for row in rows:
        for button in row:
            assert len(button_data(button)) <= 64
            assert not any(
                prefix in button.text
                for prefix in ("image_action_", "image_option_", "density_")
            )
    if page == "locale":
        selected = [b.text for row in rows for b in row if b.text.startswith("☑")]
        assert selected == ["☑ " + ("简体中文" if code == "zh-CN" else "English (US)")]


async def test_concurrent_option_changes_keep_both_remembered_selections():
    import asyncio

    repo = PreferenceRepo()
    service = ImagePreferenceService(repo)

    def change(flag):
        return lambda prefs: ImagePreferences(
            **{
                **prefs.model_dump(),
                "image_options": prefs.image_options.set_flag(flag, True),
            }
        )

    await asyncio.gather(
        service.update(1, change(ImageOption.VISUAL_PRIORITY)),
        service.update(1, change(ImageOption.REMOVE_OVERLAYS)),
    )
    flags = (await service.get(1)).image_options.flags
    assert ImageOption.VISUAL_PRIORITY in flags and ImageOption.REMOVE_OVERLAYS in flags


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_generated_visual_failed_qc_stays_review_with_no_final_asset(app, code):
    app.hub.cpa.returns.update(
        images_generate=png_bytes(), chat_json=good_qc(identifiers_consistent=False)
    )
    [result] = await Pipeline(app)._process_images(
        TASK,
        env(code, image_action="generate"),
        [],
        {},
        RewriteResult(post=POST[code], hook="Compatibility"),
        Evaluation(suitable=True, value_score=0.8),
        app.i18n.get(code),
        app.config.current,
        verified_facts=packet(code),
    )
    assert (
        result.decision is ImageDecision.REVIEW
        and result.review_blob
        and not result.output
    )
    stored = await Pipeline(app)._persist(
        TASK, [], [result], {}, app.config.current, locale=code
    )
    assert stored[0].asset_key is None and not app.storage.named("persist")


async def test_image_job_compare_and_set_rejection_does_not_enqueue(app):
    app.repo.returns.update(get_task=ready_task(), queue_image_rerun=False)
    app.workers = Recorder(enqueue=True)
    ok, _ = await Ops(app).rerun_images(
        TASK, 1, ImagePreferences(image_action="generate")
    )
    assert not ok and not app.workers.calls


@pytest.mark.parametrize(
    "action",
    [
        ImageAction.KEEP,
        ImageAction.ENHANCE,
        ImageAction.LOCALIZE,
        ImageAction.RECREATE,
        ImageAction.CLEAN_RECREATE,
    ],
)
async def test_source_dependent_action_on_text_task_returns_clear_review(app, action):
    [result] = await Pipeline(app)._process_images(
        TASK,
        env(image_action=action),
        [],
        {},
        RewriteResult(post=POST["en-US"], hook="Compatibility"),
        Evaluation(suitable=True, value_score=0.8),
        app.i18n.get("en-US"),
        app.config.current,
        verified_facts=packet(),
    )
    assert result.decision is ImageDecision.REVIEW and result.reason == t(
        "en-US", "image_source_required"
    )
    assert not app.hub.cpa.named("images_generate")


def test_authorized_information_localization_can_redesign_without_pixel_similarity():
    source = ImageAnalysis(description="UI", image_type="mixed_layout", relevance=0.9)
    opts = ImageOptions().set_flag(ImageOption.REDESIGN, True)
    result = plan(
        source,
        requested=ImageAction.LOCALIZE,
        options=opts,
        owned=True,
        target_language="zh",
        locale="zh-CN",
    )
    assert (
        result.action is ImageAction.LOCALIZE
        and result.execution == "create"
        and result.qc_mode == "regenerate"
    )
    preserved = plan(
        source,
        requested=ImageAction.LOCALIZE,
        options=ImageOptions(),
        owned=True,
        target_language="zh",
        locale="zh-CN",
    )
    assert preserved.execution == "edit" and preserved.qc_mode == "localize"
