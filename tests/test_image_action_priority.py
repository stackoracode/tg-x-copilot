"""Regression: explicit information-card creation must win over remembered cleanup flags."""

from types import SimpleNamespace

import pytest

from conftest import Recorder, png_bytes
from test_bilingual import SOURCE, good_qc
from test_image_authorization import TASK, blocked_task
from tg_x_copilot.bot.image_tools import payload
from tg_x_copilot.image_settings import (
    ImageAction,
    ImageOption,
    ImageOptions,
    ImagePreferences,
    WorkflowMode,
)
from tg_x_copilot.i18n import t
from tg_x_copilot.models import ImageQC, VerifiedFacts, VerifiedFact
from tg_x_copilot.pipeline.overlays import wants_region_edit
from tg_x_copilot.pipeline.image_policy import plan, needs_edit_confirmation
from tg_x_copilot.pipeline.processor import Pipeline
from tg_x_copilot.services.image_preferences import ImagePreferenceService


@pytest.mark.parametrize("action", [ImageAction.INFO_CARD, ImageAction.GENERATE])
@pytest.mark.parametrize("fallback", [False, True])
def test_explicit_fact_creation_overrides_cleanup_preferences_without_changing_them(
    action, fallback
):
    flags = {ImageOption.MINIMAL_CHANGES, ImageOption.REMOVE_OVERLAYS}
    if fallback:
        flags.add(ImageOption.INFO_CARD_FALLBACK)
    options = ImageOptions(flags=flags, promotion_targets={"0.tg"})
    snapshot = options.model_dump(mode="json")
    assert not wants_region_edit(action, options)
    assert options.model_dump(mode="json") == snapshot
    assert wants_region_edit(None, options)  # Switching back to AUTO restores scoped cleanup.
    assert wants_region_edit(ImageAction.ENHANCE, options)
    assert wants_region_edit(ImageAction.CLEAN_RECREATE, options)


@pytest.mark.parametrize("action", [ImageAction.OMIT, ImageAction.TEXT_ONLY])
def test_omission_does_not_request_source_editing(action):
    assert not wants_region_edit(
        action, ImageOptions(flags={ImageOption.MINIMAL_CHANGES, ImageOption.REMOVE_OVERLAYS})
    )


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
@pytest.mark.parametrize("action", [ImageAction.INFO_CARD, ImageAction.GENERATE])
@pytest.mark.parametrize("failure", [None, "image2", "qc", "upload", "fetch", "send"])
async def test_exact_production_combination_generates_qc_saves_and_sends_caption_bundle(
    app, code, action, failure
):
    b, original, analysis, image, fetch = await blocked_task(app, code)
    # The live bug had primary info_card plus these remembered flags, with no fallback option.
    envelope = original.model_copy(
        update={
            "image_action": action,
            "processing_mode": "images_only",
            "image_retry_indices": None,
        }
    )
    analysis.mark_regions[0].safe_to_remove = False
    analysis.mark_regions[0].removal_risk = "content_occluded"
    app.repo.task["envelope"] = envelope.model_dump(mode="json")
    meta = app.repo.task["draft_meta"]
    meta["source_analyses"] = {"0": analysis.model_dump(mode="json")}
    fact = "新功能支持 12 台设备。" if code == "zh-CN" else "The new feature supports 12 devices."
    meta["verified_facts"] = VerifiedFacts(
        facts=[VerifiedFact(text=fact, evidence=SOURCE)]
    ).model_dump(mode="json")
    draft = app.repo.task["draft_text"]

    def verify(model, messages, schema, **kw):
        assert schema is ImageQC, (
            "No source Vision/cleaning/fact extraction/evaluation/rewrite should be repeated"
        )
        return good_qc(numbers_consistent=failure != "qc", rendered_text="12")

    app.hub.cpa.returns.update(
        chat_json=verify,
        images_generate=(
            ConnectionError("upstream unavailable") if failure == "image2" else png_bytes("blue")
        ),
    )
    app.hub.r2.upload_fail = failure == "upload"
    app.hub.r2.fetch_fail = failure == "fetch"
    if failure == "send":
        b.client.returns["send_file"] = ConnectionError("telegram unavailable")
    await Pipeline(app)._process_images_only(
        TASK, app.repo.task, envelope, app.i18n.get(code), app.config.current
    )
    assert not fetch.named("fetch_media") and not app.hub.cpa.named("images_edit")
    assert not app.hub.jev.calls and len(app.hub.cpa.named("images_generate")) == (
        2 if failure == "qc" else 1
    )
    assert app.repo.task["draft_text"] == draft
    assert (
        app.repo.task["envelope"]["image_options"]["flags"]
        == envelope.model_dump(mode="json")["image_options"]["flags"]
    )
    # Source is deliberately omitted; the newly generated asset occupies index 1.
    assert app.repo.media[0]["decision"] == "omit"
    if failure:
        display_stage = {
            "image2": "IMAGE2",
            "qc": "IMAGE_QC",
            "upload": "R2_UPLOAD",
            "fetch": "R2_FETCH",
            "send": "TELEGRAM_SEND",
        }[failure]
        card = b.client.named("send_message")[0][0][1]
        assert "[" + display_stage + "]" in card and "[IMAGE_POLICY]" not in card
        assert app.repo.task["status"] == "needs_review"
        assert b.client.named("send_message")[-1][0][1] == draft
        assert not app.repo.task["draft_meta"]["delivery"]["1"]["sent"]
        if failure in ("image2", "qc", "upload"):
            assert not app.repo.media[1].get("asset_key")
    else:
        final = app.repo.media[1]
        assert final["asset_kind"] == "final" and final["decision"] == action.value
        sent, kw = b.client.named("send_file")[0]
        assert sent[1].getvalue() == app.hub.r2.objects[final["asset_key"]]
        assert kw["caption"] == draft and kw["parse_mode"] is None and kw["force_document"] is False
        assert [name for name, _, _ in b.client.calls] == ["send_file", "send_message"]
        assert b.client.named("send_message")[0][1]["reply_to"] == 10
        assert app.repo.task["draft_meta"]["delivery"]["1"]["sent"]
        assert app.repo.task["status"] == "draft_ready"
    assert not needs_edit_confirmation(app.repo.task, app.repo.media[0])


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_main_action_callback_keeps_preference_memory_but_honors_creation_intent(app, code):
    b, original, analysis, image, fetch = await blocked_task(app, code)
    app.config.current.default_locale = code
    app.repo.returns["get_image_preferences"] = ImagePreferences(
        image_options=original.image_options
    ).model_dump(mode="json")
    # Persist fake preferences across a service restart, as the real repository does.
    stored = {}

    async def get(user):
        return stored.get(user, app.repo.returns["get_image_preferences"])

    async def save(user, prefs):
        stored[user] = prefs

    app.repo.get_image_preferences = get
    app.repo.save_image_preferences = save
    app.image_preferences = ImagePreferenceService(app.repo)
    r = Recorder()
    event = SimpleNamespace(
        sender_id=1,
        chat_id=5,
        data=payload(TASK, "a", "info_card"),
        answer=r.answer,
        respond=r.respond,
        edit=r.edit,
    )
    await b._on_callback(event)
    prefs = await ImagePreferenceService(app.repo).get(1)
    assert prefs.image_action is ImageAction.INFO_CARD
    assert prefs.image_options == original.image_options
    assert not wants_region_edit(prefs.image_action, prefs.image_options)
    assert t(code, "image_tools_creation_priority") in r.named("edit")[0][0][0]


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_direct_plan_does_not_treat_info_card_as_source_watermark_removal(app, code):
    _, _, analysis, _, _ = await blocked_task(app, code)
    analysis.mark_regions[0].removal_risk = "content_occluded"
    options = ImageOptions(flags={ImageOption.MINIMAL_CHANGES, ImageOption.REMOVE_OVERLAYS})
    resolved = plan(
        analysis,
        requested=ImageAction.INFO_CARD,
        options=options,
        owned=False,
        target_language="en",
        locale=code,
    )
    assert resolved.execution == "create" and resolved.qc_mode == "regenerate"


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_legacy_nontraceable_packet_is_rebuilt_without_source_edit_or_text_rewrite(app, code):
    from tg_x_copilot.models import FactVerification
    from tg_x_copilot.pipeline.language import LanguageCheck

    b, original, analysis, image, fetch = await blocked_task(app, code)
    envelope = original.model_copy(
        update={
            "image_action": ImageAction.INFO_CARD,
            "processing_mode": "images_only",
            "image_retry_indices": None,
        }
    )
    app.repo.task["envelope"] = envelope.model_dump(mode="json")
    meta = app.repo.task["draft_meta"]
    meta["verified_facts"] = VerifiedFacts(
        facts=[
            VerifiedFact(text="Old promotion", evidence="no longer in clean evidence", source_idx=0)
        ]
    ).model_dump(mode="json")
    fact = "新功能支持 12 台设备。" if code == "zh-CN" else "The new feature supports 12 devices."
    packet = VerifiedFacts(facts=[VerifiedFact(text=fact, evidence=SOURCE)])
    schemas = []

    def respond(model, messages, schema, **kw):
        schemas.append(schema)
        if schema is VerifiedFacts:
            return packet
        if schema is LanguageCheck:
            return LanguageCheck(passed=True)
        if schema is FactVerification:
            return FactVerification(passed=True)
        if schema is ImageQC:
            return good_qc(rendered_text="12")
        raise AssertionError("Original text task must not run: " + schema.__name__)

    app.hub.cpa.returns.update(chat_json=respond, images_generate=png_bytes("blue"))
    draft = app.repo.task["draft_text"]
    await Pipeline(app)._process_images_only(
        TASK, app.repo.task, envelope, app.i18n.get(code), app.config.current
    )
    assert schemas == [VerifiedFacts, LanguageCheck, FactVerification, ImageQC]
    assert not fetch.named("fetch_media") and not app.hub.cpa.named("images_edit")
    assert not app.hub.jev.calls and app.repo.task["draft_text"] == draft
    assert app.repo.task["draft_meta"]["verified_facts"] == packet.model_dump(mode="json")
    assert app.repo.media[1]["asset_kind"] == "final"
    assert b.client.named("send_file")[0][1]["caption"] == draft


async def test_manual_rerun_qc_allows_numbers_from_envelope_text(app):
    b, original, analysis, image, fetch = await blocked_task(app, "zh-CN")
    envelope = original.model_copy(
        update={
            "image_action": ImageAction.GENERATE,
            "processing_mode": "images_only",
            "image_retry_indices": None,
            "text": "最新大模型2维卷3维，支持教育平权",
        }
    )
    app.repo.task["envelope"] = envelope.model_dump(mode="json")
    meta = app.repo.task["draft_meta"]
    meta["canonical_text"] = envelope.text
    meta["verified_facts"] = VerifiedFacts(
        facts=[VerifiedFact(text="支持教育平权", evidence="教育平权")]
    ).model_dump(mode="json")
    meta["source_analyses"] = {"0": analysis.model_dump(mode="json")}

    app.hub.cpa.returns.update(
        chat_json=good_qc(rendered_text="2维到3维"),
        images_generate=png_bytes("blue"),
    )
    await Pipeline(app)._process_images_only(
        TASK, app.repo.task, envelope, app.i18n.get("zh-CN"), app.config.current
    )
    assert app.repo.media[1]["asset_kind"] == "final"
    assert app.repo.task["draft_meta"]["delivery"]["1"]["sent"] is True


async def test_manual_rerun_retries_with_feedback_on_initial_qc_failure(app):
    b, original, analysis, image, fetch = await blocked_task(app, "zh-CN")
    envelope = original.model_copy(
        update={
            "image_action": ImageAction.INFO_CARD,
            "processing_mode": "images_only",
            "image_retry_indices": None,
            "workflow_mode": WorkflowMode.MANUAL,
        }
    )
    app.repo.task["envelope"] = envelope.model_dump(mode="json")
    meta = app.repo.task["draft_meta"]
    meta["canonical_text"] = SOURCE
    meta["source_analyses"] = {"0": analysis.model_dump(mode="json")}
    meta["verified_facts"] = VerifiedFacts(
        facts=[VerifiedFact(text="新功能支持 12 台设备。", evidence=SOURCE)]
    ).model_dump(mode="json")

    tries = 0

    def qc_with_retry(model, messages, schema, **kw):
        nonlocal tries
        tries += 1
        if tries == 1:
            # First attempt fails QC due to English text
            return good_qc(language_consistent=False, rendered_text="12")
        # Second attempt passes
        return good_qc(language_consistent=True, rendered_text="12")

    app.hub.cpa.returns.update(
        chat_json=qc_with_retry,
        images_generate=png_bytes("blue"),
    )
    await Pipeline(app)._process_images_only(
        TASK, app.repo.task, envelope, app.i18n.get("zh-CN"), app.config.current
    )
    assert tries == 2
    assert len(app.hub.cpa.named("images_generate")) == 2
    assert app.repo.media[1]["asset_kind"] == "final"
    assert app.repo.task["draft_meta"]["delivery"]["1"]["sent"] is True
