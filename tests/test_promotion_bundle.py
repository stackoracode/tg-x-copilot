"""Promotion-free publishing evidence, risk-aware edits and media+caption delivery."""

from types import SimpleNamespace

import pytest

from conftest import png_bytes, sent_files
from test_bilingual import SOURCE, POST, env, bot, good_qc
from test_image_delivery import setup, promotion, copyright
from tg_x_copilot.image_settings import ImageAction, ImageOption, ImageOptions
from tg_x_copilot.i18n import t
from tg_x_copilot.models import (
    ImageAnalysis,
    Evaluation,
    RewriteResult,
    VerifiedFacts,
    VerifiedFact,
    FactVerification,
)
from tg_x_copilot.pipeline.cleaning import preclean
from tg_x_copilot.pipeline.facts import build_verified_facts, packet_is_traceable
from tg_x_copilot.pipeline.image_content import editorial_text, sanitize_analysis
from tg_x_copilot.pipeline.image_policy import plan
from tg_x_copilot.pipeline.language import LanguageCheck
from tg_x_copilot.pipeline.overlays import approved_regions
from tg_x_copilot.pipeline.processor import Pipeline
from tg_x_copilot.pipeline.guards import XRules

PROMO = "TG: 互联网从业者充电站 @https1024"
CORE = "Acme X12 supports 12 devices on 2026-10-05, using HTTP/3."


def analysis():
    return ImageAnalysis(
        description="Acme X12 interface",
        image_type="ui_screenshot",
        relevance=0.95,
        contains_text=True,
        text_language="en",
        extracted_text=CORE + "\n" + PROMO + "\n© Photographer",
        source_facts=[CORE, "底部叠加文字“" + PROMO + "”。"],
        has_channel_overlay=True,
        has_source_copyright_mark=True,
        mark_regions=[
            promotion().model_copy(update={"text": PROMO, "safe_to_remove": False}),
            copyright(),
        ],
    )


@pytest.mark.parametrize("at_start", [True, False])
def test_obvious_tg_header_or_footer_is_removed_before_jev_with_core_unchanged(at_start):
    text = (PROMO + "\n" + CORE) if at_start else (CORE + "\n" + PROMO)
    assert (
        preclean(text + "\nhttps://github.com/acme/x12") == CORE + "\nhttps://github.com/acme/x12"
    )


def test_image_ocr_splits_promotion_and_attribution_from_facts_without_mutating_qc_source():
    a = analysis()
    clean = sanitize_analysis(a)
    assert clean.content_text == CORE and editorial_text(clean) == CORE
    assert clean.source_facts == [CORE]
    assert clean.extracted_text == a.extracted_text
    assert clean.mark_regions == a.mark_regions
    assert (
        clean.mark_regions[0].safe_to_remove is False
    )  # Text cleaning never grants pixel-edit permission.
    old = VerifiedFacts(facts=[VerifiedFact(text=PROMO, evidence=PROMO, source_idx=0)])
    assert not packet_is_traceable(old, "", {0: a})
    valid = VerifiedFacts(facts=[VerifiedFact(text=CORE, evidence=CORE, source_idx=0)])
    assert packet_is_traceable(valid, "", {0: a})


async def test_fact_packet_and_evaluation_receive_no_image_promotion(app):
    a = analysis()
    envelope = env()
    envelope.text = ""

    def respond(model, messages, schema, **kw):
        assert "@https1024" not in str(messages)
        if schema is VerifiedFacts:
            return VerifiedFacts(facts=[VerifiedFact(text=CORE, evidence=CORE, source_idx=0)])
        if schema is FactVerification:
            return FactVerification(passed=True)
        if schema is LanguageCheck:
            return LanguageCheck(passed=True)
        if schema is Evaluation:
            return Evaluation(suitable=True, value_score=0.9)
        raise AssertionError(schema)

    app.hub.cpa.returns["chat_json"] = respond
    packet = await build_verified_facts(envelope, {0: a}, app)
    assert packet.facts[0].text == CORE
    from tg_x_copilot.models import TriageResult

    triage = TriageResult(
        route="proceed", value=0.9, confidence=0.9, content_type="practical", use_media=True
    )
    await Pipeline(app)._evaluate(
        envelope, triage, {0: a}, app.i18n.get("en-US"), app.config.current
    )


async def test_rewrite_cannot_reintroduce_identified_channel_promotion(app):
    # English draft containing Chinese promotion also fails language validation; use zh-CN.
    envelope = env("zh-CN")
    a = analysis()
    app.hub.cpa.returns["chat_json"] = lambda model, messages, schema, **kw: (
        RewriteResult(post="新功能支持 12 台设备。" + PROMO, hook="新功能")
        if schema is RewriteResult
        else LanguageCheck(passed=True)
    )
    _, report = await Pipeline(app)._rewrite(
        "t",
        envelope,
        Evaluation(suitable=True, value_score=0.9),
        {0: a},
        XRules(),
        [],
        app.i18n.get("zh-CN"),
        app.config.current,
    )
    assert t("zh-CN", "guard_promotion") in report.problems


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
def test_promotion_on_empty_ui_background_is_allowed_only_after_rights_confirmation(code):
    a = analysis()
    a.mark_regions[0] = a.mark_regions[0].model_copy(
        update={
            "safe_to_remove": True,
            "removal_risk": "background_only",
            "removal_reason": "Plain background",
        }
    )
    options = ImageOptions(flags={ImageOption.MINIMAL_CHANGES, ImageOption.REMOVE_OVERLAYS})
    assert approved_regions(a, options, 0) == [a.mark_regions[0]]
    assert (
        plan(
            a, requested=None, options=options, owned=False, target_language="en", locale=code
        ).execution
        == "review"
    )
    resolved = plan(
        a, requested=None, options=options, owned=True, target_language="en", locale=code
    )
    assert resolved.execution == "edit" and resolved.qc_mode == "promotion_cleanup"


@pytest.mark.parametrize("risk", ["content_occluded", "uncertain"])
@pytest.mark.parametrize("allow", [True, False])
@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
def test_unsafe_cleanup_has_specific_reason_or_explicit_original_card_fallback(risk, allow, code):
    a = analysis()
    a.mark_regions[0].removal_risk = risk
    flags = {ImageOption.MINIMAL_CHANGES, ImageOption.REMOVE_OVERLAYS}
    if allow:
        flags.add(ImageOption.INFO_CARD_FALLBACK)
    resolved = plan(
        a,
        requested=None,
        options=ImageOptions(flags=flags),
        owned=True,
        target_language="en",
        locale=code,
    )
    if allow:
        assert resolved.action is ImageAction.INFO_CARD and resolved.execution == "create"
    else:
        key = (
            "cleanup_content_occluded" if risk == "content_occluded" else "cleanup_region_uncertain"
        )
        assert resolved.execution == "review" and resolved.reason == t(code, key)


@pytest.mark.parametrize("unsafe", ["unowned", "protected_target", "sensitive"])
def test_card_fallback_never_overrides_missing_rights_or_protected_target(unsafe):
    a = analysis()
    a.mark_regions[0].removal_risk = "content_occluded"
    if unsafe == "sensitive":
        a.sensitive = True
    options = ImageOptions(
        flags={
            ImageOption.MINIMAL_CHANGES,
            ImageOption.REMOVE_OVERLAYS,
            ImageOption.INFO_CARD_FALLBACK,
        },
        promotion_targets={"0.credit"} if unsafe == "protected_target" else set(),
    )
    resolved = plan(
        a,
        requested=None,
        options=options,
        owned=unsafe != "unowned",
        target_language="en",
        locale="en-US",
    )
    assert resolved.execution == "review"


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_permitted_fallback_generates_fact_checked_card_without_editing_source(app, code):
    envelope, a, image = setup(app, code, cleanup=True)
    a.mark_regions[0].safe_to_remove = False
    a.mark_regions[0].removal_risk = "content_occluded"
    envelope.image_options = envelope.image_options.set_flag(ImageOption.INFO_CARD_FALLBACK, True)
    app.hub.cpa.returns.update(
        images_generate=png_bytes("blue"), chat_json=good_qc(rendered_text="12")
    )
    [outcome] = await Pipeline(app)._process_images(
        "a" * 32,
        envelope,
        [image],
        {0: a},
        RewriteResult(post=POST[code], hook="Compatibility" if code == "en-US" else "兼容性"),
        Evaluation(suitable=True, value_score=0.9),
        app.i18n.get(code),
        app.config.current,
        verified_facts=VerifiedFacts(facts=[VerifiedFact(text=SOURCE, evidence=SOURCE)]),
    )
    assert outcome.output and outcome.decision.value == "info_card"
    assert not app.hub.cpa.named("images_edit")
    assert len(app.hub.cpa.named("images_generate")) == 1
    assert app.hub.cpa.named("chat_json")  # No output is final without Vision QC.


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
@pytest.mark.parametrize("count", [1, 3, 11])
async def test_picture_and_draft_are_one_content_bundle_then_related_review(app, code, count):
    task = {
        "id": "a" * 32,
        "locale": code,
        "tg_chat_id": 5,
        "status": "draft_ready",
        "draft_text": POST[code],
        "score": 0.9,
        "draft_meta": {},
    }
    app.repo.returns.update(
        get_task=task,
        list_media=[
            {"idx": i, "decision": "keep", "asset_kind": "final", "asset_key": f"{i}.png"}
            for i in range(count)
        ],
    )
    app.hub.r2.returns["get_object"] = png_bytes()
    b = bot(app)
    await b.send_draft(task["id"])
    sent = b.client.named("send_file")
    assert [len(sent_files(args[1])) for args, _ in sent] == ([10, 1] if count == 11 else [count])
    firstcaption = sent[0][1]["caption"]
    assert (firstcaption[0] if isinstance(firstcaption, list) else firstcaption) == POST[code]
    if isinstance(firstcaption, list):
        assert firstcaption[1:] == [""] * (min(count, 10) - 1)
    if count == 1:
        assert not isinstance(sent[0][0][1], list)
    assert sent[0][1]["parse_mode"] is None and not sent[0][1]["force_document"]
    if count == 11:
        assert sent[1][1]["caption"] == ""
    messages = b.client.named("send_message")
    assert len(messages) == 1 and messages[0][1]["reply_to"] == 1
    assert POST[code] not in messages[0][0][1]  # No duplicated draft/status pollution.
    assert [n for n, _, _ in b.client.calls][-1] == "send_message"


@pytest.mark.parametrize("draft", ["x" * 1025, "😀" * 513])
async def test_oversize_caption_is_not_truncated_and_is_sent_as_related_reply(app, draft):
    app.repo.returns.update(
        get_task={
            "id": "t",
            "locale": "en-US",
            "tg_chat_id": 5,
            "status": "draft_ready",
            "draft_text": draft,
            "draft_meta": {},
        },
        list_media=[{"idx": 0, "decision": "keep", "asset_kind": "final", "asset_key": "a"}],
    )
    app.hub.r2.returns["get_object"] = png_bytes()
    b = bot(app)
    await b.send_draft("t")
    assert b.client.named("send_file")[0][1]["caption"] == ""
    last, kwargs = b.client.named("send_message")[-1]
    assert last[1] == draft and kwargs["reply_to"] == 1 and kwargs["parse_mode"] is None


async def test_unconfirmed_first_photo_caption_does_not_lose_draft(app):
    app.repo.returns.update(
        get_task={
            "id": "t",
            "locale": "en-US",
            "tg_chat_id": 5,
            "status": "draft_ready",
            "draft_text": "Complete draft",
            "draft_meta": {},
        },
        list_media=[
            {"idx": i, "decision": "keep", "asset_kind": "final", "asset_key": str(i)}
            for i in range(2)
        ],
    )
    app.hub.r2.returns["get_object"] = png_bytes()
    b = bot(app)
    b.client.returns["send_file"] = [
        SimpleNamespace(photo=None, id=1),
        SimpleNamespace(photo=object(), id=2),
    ]
    await b.send_draft("t")
    assert b.client.named("send_message")[-1][0][1] == "Complete draft"
    assert b.client.named("send_message")[-1][1]["reply_to"] == 2
    assert "[TELEGRAM_SEND]" in b.client.named("send_message")[0][0][1]


def test_split_ocr_promotion_handles_do_not_leak_and_protected_names_stay_in_source():
    from tg_x_copilot.pipeline.image_content import contains_promotion

    a = analysis()
    a.extracted_text = CORE + "\nTG: 互联网从业者充电站\n@https1024\n© Photographer"
    clean = sanitize_analysis(a)
    assert "@https1024" not in clean.content_text
    assert contains_promotion("关注 @https1024", a)
    assert not contains_promotion("Acme X12 / HTTP/3", a)
    assert "© Photographer" in clean.extracted_text


@pytest.mark.parametrize("code", ["en-US", "zh-CN"])
async def test_new_fallback_option_is_composable_and_remembered_via_callback(app, code):
    from test_image_tools import PreferenceRepo, ready_task, TASK
    from tg_x_copilot.services.image_preferences import ImagePreferenceService
    from tg_x_copilot.bot.image_tools import payload
    from conftest import Recorder

    repo = PreferenceRepo()
    repo.returns["get_task"] = ready_task(code)
    app.repo = repo
    app.image_preferences = ImagePreferenceService(repo)
    app.ops = Recorder()
    b = bot(app)
    for value in ("minimal_changes,1", "remove_overlays,1", "info_card_fallback,1"):
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
    prefs = await ImagePreferenceService(repo).get(1)
    assert {
        ImageOption.MINIMAL_CHANGES,
        ImageOption.REMOVE_OVERLAYS,
        ImageOption.INFO_CARD_FALLBACK,
    } <= prefs.image_options.flags
