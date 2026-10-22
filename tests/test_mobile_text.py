"""One mobile image end-to-end: mode selection, full-source briefing, layout and original delivery."""

from types import SimpleNamespace

import pytest
from conftest import Recorder, sent_files
from test_bilingual import bot
from test_image_delivery import process, setup
from test_image_tools import PreferenceRepo, button_data
from test_quick_and_links import (
    POSTS,
    SOURCE,
    TASK_ID,
    make_client,
    saved,
    source_env,
    task_of,
)
from test_quick_and_links import quick_app as _quick_app

from tg_x_copilot.app_context import submit_envelope
from tg_x_copilot.bot.image_tools import ImageTools, keyboard
from tg_x_copilot.image_settings import (
    ImageAction,
    ImagePreferences,
    InformationDensity,
    WorkflowMode,
    resolve_workflow,
)
from tg_x_copilot.models import InputEnvelope, TaskStatus
from tg_x_copilot.pipeline.media import inspect_image
from tg_x_copilot.pipeline.mobile_text import (
    CardSection,
    MobileCardPlan,
    fallback_plan,
    render_mobile_card,
)
from tg_x_copilot.pipeline.processor import Pipeline
from tg_x_copilot.services.image_preferences import ImagePreferenceService
from tg_x_copilot.services.link_reader import LinkReader

quick_app = _quick_app


def outline(code="en-US"):
    return MobileCardPlan(title="Device information" if code == "en-US" else "设备重点信息",
        intro=POSTS[code], sections=[CardSection(title="Details" if code == "en-US" else "关键数据",
                                               points=[SOURCE if code == "en-US" else POSTS[code]])])


def test_web_exposes_a_third_mode_and_remembers_it():
    client, _ = make_client()
    client.app.state.ctx.image_preferences.returns['get'] = ImagePreferences(workflow_mode=WorkflowMode.MOBILE_TEXT)
    r = client.get('/new')
    assert r.status_code == 200
    assert 'name="mode" value="mobile_text" checked' in r.text
    assert '9:16' in r.text and 'never multiple images' in r.text


def test_web_submission_selects_exactly_one_high_density_mobile_image():
    client, submitted = make_client()
    r = client.post('/new', data={'mode': 'mobile_text', 'text': SOURCE, 'density': 'low',
                                 'image_action': 'omit'}, follow_redirects=False)
    assert r.status_code == 303
    prefs = submitted['prefs']
    assert prefs.workflow_mode == WorkflowMode.MOBILE_TEXT
    assert prefs.image_action == ImageAction.MOBILE_TEXT
    assert prefs.image_options.information_density == InformationDensity.HIGH


async def test_telegram_button_saves_mode_and_intake_resolves_to_mobile_image(app):
    app.image_preferences = ImagePreferenceService(PreferenceRepo())
    b = bot(app)
    rows = keyboard('-', ImagePreferences(), b.t)
    assert any(button_data(button) == b'it:-:w:mobile_text' for row in rows for button in row)
    event = SimpleNamespace(sender_id=7, chat_id=5, answer=Recorder().answer,
                            edit=Recorder().edit, respond=Recorder().respond)
    await ImageTools(b).handle(event, 'it:-:w:mobile_text')
    prefs = await app.image_preferences.get(7)
    assert prefs.workflow_mode == WorkflowMode.MOBILE_TEXT
    app.repo.returns['create_task'] = TASK_ID
    app.workers = Recorder()
    env = InputEnvelope(chat_id=5, user_id=7, message_ids=[1], text=SOURCE)
    await submit_envelope(app, env, prefs)
    snapshot = app.repo.named('create_task')[0][0][0]
    assert snapshot.quick and snapshot.image_action == ImageAction.MOBILE_TEXT
    assert snapshot.image_options.information_density == InformationDensity.HIGH
    await ImageTools(b).handle(event, 'it:-:w:manual')
    normal = resolve_workflow(await app.image_preferences.get(7), has_images=False)
    assert normal.workflow_mode == WorkflowMode.MANUAL and normal.image_action is None


@pytest.mark.parametrize('code', ['en-US', 'zh-CN'])
async def test_full_pipeline_returns_one_phone_image_and_the_standard_post(quick_app, code):
    quick_app.config.current.default_locale = code
    quick_app.test_responses[MobileCardPlan] = outline(code)
    envelope = source_env(code, quick=False, workflow_mode=WorkflowMode.MOBILE_TEXT)
    await Pipeline(quick_app)._process_claimed(TASK_ID, task_of(envelope))
    status, post, meta = saved(quick_app)
    assert status == TaskStatus.DRAFT_READY and post == POSTS[code]
    assert meta['mobile_card']['width'] == 1440 and meta['mobile_card']['height'] == 2560
    assert meta['mobile_card']['image_count'] == 1
    assert len([m for m in meta['media'] if m['asset_kind'] == 'final']) == 1
    assert not quick_app.hub.cpa.named('images_generate') and not quick_app.hub.cpa.named('images_edit')
    (_task, _idx, blob, _kind), _ = quick_app.storage.named('persist')[-1]
    assert (blob.width, blob.height) == (1440, 2560)
    assert not meta['problems'] and not meta['review']


async def test_long_source_ending_reaches_image_brief_even_when_post_cleaning_shortens_it(quick_app):
    article = SOURCE + '\n' + ('Background paragraph.\n' * 1600) + 'IMPORTANT END RESULT: 77 devices.'
    quick_app.test_responses[MobileCardPlan] = outline()
    env = source_env(text=article, message_texts=[article], is_direct=False,
                     workflow_mode=WorkflowMode.MOBILE_TEXT)
    await Pipeline(quick_app)._process_claimed(TASK_ID, task_of(env))
    _, _, meta = saved(quick_app)
    assert meta['canonical_text'] == SOURCE
    assert meta['card_source_text'].endswith('IMPORTANT END RESULT: 77 devices.')
    prompt = next(a[1][-1]['content'] for a, _ in quick_app.hub.cpa.named('chat_json') if a[2] is MobileCardPlan)
    assert 'IMPORTANT END RESULT: 77 devices.' in prompt


def test_dense_layout_keeps_one_fixed_canvas_and_omits_whole_lower_priority_points():
    plan = outline('zh-CN')
    plan.sections += [CardSection(title='后续资料', points=['后续补充内容。' * 12] * 50)]
    rendered = render_mobile_card(plan, 'zh-CN')
    image = inspect_image(rendered.data)
    assert image and (image.width, image.height) == (1440, 2560)
    assert rendered.omitted_points > 0
    assert POSTS['zh-CN'].replace(' ', '') in rendered.text.replace('\n', '').replace(' ', '')


async def test_outline_failure_automatically_uses_source_key_sentences(quick_app):
    quick_app.test_responses[MobileCardPlan] = RuntimeError('model unavailable')
    env = source_env(image_action=ImageAction.MOBILE_TEXT)
    await Pipeline(quick_app)._process_claimed(TASK_ID, task_of(env))
    status, _, meta = saved(quick_app)
    assert status == TaskStatus.DRAFT_READY and meta['mobile_card']['image_count'] == 1
    assert not meta['problems'] and not meta['review']


def test_mobile_reader_limit_is_scoped_to_this_read_and_shares_browser_capacity(app):
    reader = LinkReader(app)
    expanded = reader.with_max_chars(120000)
    assert reader._cfg.fetch_max_chars == 20000
    assert expanded._cfg.fetch_max_chars == 120000
    assert reader._browser_slot is expanded._browser_slot
    assert app.config.current.web.fetch_max_chars == 20000


async def test_telegram_delivers_mobile_mode_once_as_original_png_document(app):
    env, analysis, image = setup(app)
    await process(app, env, analysis, image)
    app.repo.task['draft_meta']['mobile_card'] = {'image_count': 1, 'width': 1440, 'height': 2560}
    b = bot(app)
    b.client.returns['send_file'] = SimpleNamespace(id=20, photo=None, document=object())
    await b.send_draft('a' * 32)
    calls = b.client.named('send_file')
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs['force_document'] is True and len(sent_files(args[1])) == 1
    assert sent_files(args[1])[0].name.endswith('.png')
    assert app.repo.task['draft_meta']['delivery']['0']['sent'] is True


def test_fallback_keeps_source_numbers_instead_of_inventing_missing_information():
    plan = fallback_plan(POSTS['en-US'], SOURCE, 'en-US')
    assert plan.intro == POSTS['en-US']
    assert all(p in SOURCE for section in plan.sections for p in section.points)


def test_phone_layout_does_not_orphan_a_closing_punctuation_mark():
    plan = MobileCardPlan(title="手机阅读", sections=[CardSection(title="阅读体验", points=[
        "图片固定为1440×2560竖版，适合手机打开后全屏阅读。",
        "优先呈现结论、时间、关键数字、比较结果和适用范围。"])] )
    rendered = render_mobile_card(plan, 'zh-CN')
    assert all(line.strip() not in {"。", "，", ".", ")", "）"} for line in rendered.text.splitlines())


async def test_mobile_image_only_rerun_retains_post_and_full_source(quick_app):
    quick_app.test_responses[MobileCardPlan] = outline()
    source = SOURCE + '\nFull source ending: 77 devices.'
    env = source_env(image_action=ImageAction.MOBILE_TEXT, processing_mode='images_only')
    task = task_of(env, draft_text='Unchanged existing post', draft_meta={
        'quick': True, 'card_source_text': source, 'canonical_text': SOURCE,
        'text_review': [], 'text_warnings': [], 'problems': []})
    await Pipeline(quick_app)._process_claimed(TASK_ID, task)
    status, text, meta = saved(quick_app)
    assert status == TaskStatus.DRAFT_READY and text == 'Unchanged existing post'
    assert meta['mobile_card']['image_count'] == 1 and meta['card_source_text'] == source
    calls = quick_app.hub.cpa.named('chat_json')
    assert len(calls) == 1 and calls[0][0][2] is MobileCardPlan
    assert 'Full source ending: 77 devices.' in calls[0][0][1][-1]['content']


async def test_multiple_input_photos_still_produce_only_one_mobile_output(quick_app):
    from conftest import png_bytes

    from tg_x_copilot.models import MediaKind, SourceMedia

    quick_app.test_responses[MobileCardPlan] = outline()
    quick_app.telegram = Recorder(fetch_media={10: png_bytes('red'), 11: png_bytes('blue')})
    env = source_env(workflow_mode=WorkflowMode.MOBILE_TEXT,
                     media=[SourceMedia(message_id=i, kind=MediaKind.PHOTO) for i in (10, 11)])
    await Pipeline(quick_app)._process_claimed(TASK_ID, task_of(env))
    status, _, meta = saved(quick_app)
    assert status == TaskStatus.DRAFT_READY
    assert [m['decision'] for m in meta['media']] == ['omit', 'omit', 'mobile_text']
    assert len(quick_app.storage.named('persist')) == 1
