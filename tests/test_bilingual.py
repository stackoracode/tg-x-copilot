"""Bilingual integration coverage uses isolated fakes; no production/network writes."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from string import Formatter
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from conftest import Recorder, png_bytes, sent_files
from tg_x_copilot import prompts
from tg_x_copilot.bot.telegram import TelegramBot
from tg_x_copilot.config import JevSettings
from tg_x_copilot.i18n import I18n, t
from tg_x_copilot.models import (
    Evaluation, ForwardOrigin, ImageAnalysis, ImageDecision, ImageQC, InputEnvelope,
    MediaKind, RewriteResult, SourceMedia, VerifiedFact, VerifiedFacts, FactVerification,
)
from tg_x_copilot.pipeline.cleaning import CoreContent, clean_bundle, preclean
from tg_x_copilot.pipeline.guards import XRules, check_rewrite, wrong_language
from tg_x_copilot.pipeline.image_policy import decide
from tg_x_copilot.pipeline.image_qc import qc_verdict
from tg_x_copilot.pipeline.language import LanguageCheck, is_localized
from tg_x_copilot.pipeline.media import inspect_image
from tg_x_copilot.pipeline.processor import LoadedImage, Pipeline
from tg_x_copilot.pipeline.triage import build_state, decide_route
from tg_x_copilot.services.config_service import ConfigService
from tg_x_copilot.services.ops import Ops
from tg_x_copilot.web.admin import require_admin, router
from test_triage import resp

LOCALES = ['en-US', 'zh-CN']
SOURCE = 'A new feature supports 12 devices, announced on 2026-10-05.'
POST = {'en-US': 'Check compatibility before upgrading: the new feature supports 12 devices.',
        'zh-CN': '升级前先核对兼容性：新功能支持 12 台设备，确认适配范围再使用。'}


def env(code='en-US', **kw):
    return InputEnvelope(chat_id=5, user_id=1, message_ids=[10], text=SOURCE, locale=code, **kw)


def bot(app):
    b = TelegramBot.__new__(TelegramBot)
    b.app, b.client, b._allowed = app, Recorder(), {1}
    return b


def good_qc(**kw):
    values = dict(passed=True, text_consistent=True, language_consistent=True,
                  numbers_consistent=True, dates_consistent=True, names_consistent=True, brands_consistent=True, identifiers_consistent=True, readability_ok=True, density_consistent=True,
                  people_consistent=True, watermarks_ok=True, facts_consistent=True)
    return ImageQC(**{**values, **kw})


def test_catalogs_have_identical_keys_and_format_parameters():
    catalog = I18n()
    assert catalog.codes == LOCALES
    en, zh = catalog.get('en-US').ui, catalog.get('zh-CN').ui
    assert en.keys() == zh.keys()
    for key in en:
        params = lambda value: {name for _, name, _, _ in Formatter().parse(value) if name}
        assert params(en[key]) == params(zh[key]), key
        assert zh[key].strip(), key


@pytest.mark.parametrize('code', LOCALES)
def test_all_editorial_prompts_and_knowledge_are_locale_specific(code):
    root = Path(prompts.__file__).parent
    names = ['extract_core', 'evaluate', 'rewrite', 'vision_analyze', 'image_enhance',
             'image_localize', 'image_regenerate', 'image_qc', 'language_check', 'image_execute', 'image_cleanup', 'image_retry', 'auto_evaluate', 'verified_facts', 'verify_facts']
    values = {key: 'test' for key in (
        'market', 'language_name', 'strings', 'text', 'triage', 'urls', 'image_notes',
        'source_info', 'max_chars', 'max_hashtags', 'max_emojis', 'style', 'banned_phrases',
        'hooks', 'angle', 'audience', 'key_facts', 'background_points', 'risks', 'feedback',
        'size', 'post', 'brief', 'facts', 'mode', 'mode_rules', 'reference_text', 'images_note', 'density', 'density_rules', 'target_locale', 'layout', 'action', 'action_rules', 'options', 'sources', 'packet', 'cleanup_contract', 'failure')}
    for name in names:
        assert (root / code / f'{name}.md').exists()
        rendered = prompts.render(name, code, **values)
        assert not re.search(r'\$[a-z_]+', rendered.system + rendered.user), name
    data = prompts.render_json('knowledge', code)
    assert data['hooks'] and data['rules']['banned_phrases']
    if code == 'zh-CN':
        assert not wrong_language(data['rules']['style'], code)
        assert all(not wrong_language(h['pattern'], code) for h in data['hooks'])


@pytest.mark.parametrize('code', LOCALES)
async def test_clean_each_message_then_canonical_bundle_preserves_audit_and_links(app, code):
    inputs = ['Forwarded from Spam Channel\n' + SOURCE + '\nJoin our channel https://t.me/spam',
              'Second fact: 3 modes.\nContact: @sales\nhttps://example.org/report?utm_source=tg&id=9']
    original = env(code, message_texts=inputs, forwards=[ForwardOrigin(sender_name='Spam Channel')])
    def extract(model, messages, schema, **kwargs):
        assert schema is CoreContent
        text = messages[-1]['content']
        assert 'Forwarded from' not in text and 'Join our' not in text and 'Contact:' not in text
        assert 'utm_source' not in text
        return CoreContent(core_text=SOURCE if '12 devices' in text else
                           'Second fact: 3 modes.\nhttps://example.org/report?id=9')
    app.hub.cpa.returns['chat_json'] = extract
    cleaned, fallback = await clean_bundle(original, app)
    assert not fallback and len(app.hub.cpa.named('chat_json')) == 2
    assert cleaned.text == SOURCE + '\n\nSecond fact: 3 modes.\nhttps://example.org/report?id=9'
    assert cleaned.urls == ['https://example.org/report?id=9']
    assert original.message_texts == inputs and original.forwards[0].sender_name == 'Spam Channel'
    state = build_state(cleaned, Pipeline._source_info(cleaned))
    assert 'Spam Channel' not in json.dumps(state) and 'utm_source' not in json.dumps(state)


@pytest.mark.parametrize('failure', [RuntimeError('unavailable'), CoreContent(core_text='999 devices')])
async def test_extraction_failures_do_not_invent_facts_or_block(app, failure):
    app.hub.cpa.returns['chat_json'] = failure
    cleaned, fallback = await clean_bundle(env(message_texts=[SOURCE + '\n关注本频道 https://t.me/promo']), app)
    assert fallback and cleaned.text == SOURCE


async def test_extraction_cancellation_propagates(app):
    app.hub.cpa.returns['chat_json'] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await clean_bundle(env(), app)


def test_cleaning_preserves_news_subjects_and_deduplicates_tags():
    text = 'Apple announced 12 devices.\nThe channel BBC reported the launch.\n#AI #AI #GPU\n投稿：@sales'
    cleaned = preclean(text)
    assert 'Apple' in cleaned and 'BBC' in cleaned
    assert cleaned.count('#AI') == 1 and '#GPU' in cleaned and '投稿' not in cleaned


@pytest.mark.parametrize('code', LOCALES)
def test_jev_reasons_follow_locale_without_changing_routes(code):
    result = decide_route(resp(3.0, .9, risky=.8), JevSettings(), has_text=True,
                          has_media=True, locale=code)
    assert result.route == 'review'
    assert not any(wrong_language(reason, code) for reason in result.reasons)
    if code == 'zh-CN':
        assert '风险' in result.reasons[0]
    skip = decide_route(resp(3, .9, promo=.95), JevSettings(), has_text=True,
                        has_media=False, locale=code)
    assert skip.route == 'skip' and ('推广' in skip.reasons[0] if code == 'zh-CN' else
                                    'Promotional' in skip.reasons[0])


@pytest.mark.parametrize('code', LOCALES)
async def test_rewrite_retries_wrong_language_and_never_publishes_it(app, code):
    app.config.current.default_locale = code
    good = RewriteResult(post=POST[code], hook='核对兼容性' if code == 'zh-CN' else 'Check compatibility',
                         added_value='提醒核对设备适配。' if code == 'zh-CN' else 'A practical compatibility check.',
                         image_brief='设备兼容性信息卡片。' if code == 'zh-CN' else 'A compatibility information card.')
    bad = good.model_copy(update={'post': POST['en-US' if code == 'zh-CN' else 'zh-CN']})
    attempts = iter([bad, good])
    def chat(model, messages, schema, **kwargs):
        return next(attempts) if schema is RewriteResult else LanguageCheck(passed=True)
    app.hub.cpa.returns['chat_json'] = chat
    result, report = await Pipeline(app)._rewrite('t1', env(code), Evaluation(suitable=True, value_score=.8),
                                                  {}, XRules(), [], app.i18n.get(code), app.config.current)
    assert result.post == POST[code] and not report.problems
    assert len([a for a, _ in app.hub.cpa.named('chat_json') if a[2] is RewriteResult]) == 2


@pytest.mark.parametrize('code', LOCALES)
async def test_semantic_language_verification_fails_closed(app, code):
    # Short prose may evade the script heuristic; the typed language verifier still rejects it.
    app.hub.cpa.returns['chat_json'] = LanguageCheck(passed=False)
    assert not await is_localized(['Not supported' if code == 'zh-CN' else '请审核'], app.i18n.get(code), app)
    app.hub.cpa.returns['chat_json'] = RuntimeError('unavailable')
    with pytest.raises(RuntimeError):
        await is_localized(['Not supported'], app.i18n.get(code), app)


@pytest.mark.parametrize('code', LOCALES)
def test_guard_diagnostics_and_qc_language_are_localized(code):
    r = RewriteResult(post='999999', hook='', added_value='')
    report = check_rewrite(r, source_text=SOURCE, rules=XRules(), locale=code)
    assert report.problems
    assert all(not wrong_language(p, code) for p in report.problems)
    if code == 'zh-CN':
        assert all(re.search(r'[\u3400-\u9fff]', p) for p in report.problems)
    passed, reason = qc_verdict(good_qc(language_consistent=False), allowed_texts=[SOURCE], locale=code)
    assert not passed and ('语言' in reason if code == 'zh-CN' else 'language' in reason)
    assert not qc_verdict(good_qc(rendered_text='99'), allowed_texts=[SOURCE], locale=code)[0]


@pytest.mark.parametrize('code', LOCALES)
@pytest.mark.parametrize('kind', ['screenshot', 'infographic', 'photo_real_event'])
async def test_third_party_overlay_or_news_yields_original_verified_bundle(app, code, kind):
    app.config.current.default_locale = code
    analysis = ImageAnalysis(description='原始信息' if code == 'zh-CN' else 'Source information',
                             image_type=kind, relevance=.9, has_channel_overlay=True,
                             contains_text=True, text_language='en' if code == 'zh-CN' else 'zh',
                             extracted_text='Acme supports 12 devices on 2026-10-05.',
                             source_facts=['Acme 支持 12 台设备，日期 2026-10-05。'] if code == 'zh-CN' else
                                          ['Acme supports 12 devices on 2026-10-05.'], brand_names=['Acme'])
    image = LoadedImage(0, inspect_image(png_bytes()), SourceMedia(message_id=10, kind=MediaKind.PHOTO))
    app.hub.cpa.returns.update(images_generate=png_bytes(), chat_json=good_qc(rendered_text='12'))
    rewrite = RewriteResult(post=POST[code], hook='hook')
    [output] = await Pipeline(app)._process_images('t1', env(code, forwards=[ForwardOrigin(chat_id=-10099)]),
        [image], {0: analysis}, rewrite, Evaluation(suitable=True, value_score=.8), app.i18n.get(code), app.config.current,
        verified_facts=VerifiedFacts(facts=[VerifiedFact(text=analysis.extracted_text, evidence=analysis.extracted_text, source_idx=0)]))
    assert output.decision is (ImageDecision.INFO_CARD if kind == 'photo_real_event' else ImageDecision.RECREATE) and output.output and output.ai_generated
    assert not app.hub.cpa.named('images_edit')
    generation = app.hub.cpa.named('images_generate')[0][0][1]
    assert 'Acme' in generation and '2026-10-05' in generation
    assert ('简体中文' in generation and '非纪实' in generation) if code == 'zh-CN' else 'non-documentary' in generation
    qc_messages = app.hub.cpa.named('chat_json')[0][0][1]
    assert len([p for p in qc_messages[-1]['content'] if p['type'] == 'image_url']) == 2
    assert not wrong_language(output.reason, code)


@pytest.mark.parametrize('code', LOCALES)
async def test_localization_uses_edit_only_with_confirmed_rights(app, code):
    analysis = ImageAnalysis(description='visual', image_type='infographic', relevance=.9,
                             contains_text=True, text_language='en' if code == 'zh-CN' else 'zh',
                             extracted_text='Acme 12', brand_names=['Acme'])
    image = LoadedImage(0, inspect_image(png_bytes()), SourceMedia(message_id=10, kind=MediaKind.PHOTO))
    app.hub.cpa.returns.update(images_edit=png_bytes(), images_generate=png_bytes(), chat_json=good_qc())
    pipeline = Pipeline(app)
    for owned in (False, True):
        app.config.current.pipeline.direct_uploads_owned = owned
        [result] = await pipeline._process_images('t1', env(code), [image], {0: analysis},
            RewriteResult(post=POST[code], hook='hook'), Evaluation(suitable=True, value_score=.8),
            app.i18n.get(code), app.config.current,
            verified_facts=VerifiedFacts(facts=[VerifiedFact(text=analysis.extracted_text, evidence=analysis.extracted_text, source_idx=0)]))
        assert result.decision is ImageDecision.LOCALIZE and result.output
    assert len(app.hub.cpa.named('images_edit')) == len(app.hub.cpa.named('images_generate')) == 1


def test_normal_brand_logo_does_not_trigger_watermark_policy():
    analysis = ImageAnalysis(description='Product', image_type='photo_generic', relevance=.9,
                             brand_names=['Acme'], has_channel_overlay=False, has_third_party_watermark=False)
    assert decide(analysis, owned_source=True, target_language='en')[0] is ImageDecision.KEEP
    assert decide(analysis, owned_source=False, target_language='en')[0] is ImageDecision.RECREATE


@pytest.mark.parametrize('code', LOCALES)
async def test_telegram_delivers_all_images_draft_buttons_and_localized_reasons(app, code):
    # Task language stays consistent even if global setting changes while the worker runs.
    app.config.current.default_locale = 'en-US' if code == 'zh-CN' else 'zh-CN'
    task = {'id':'t1', 'tg_chat_id':5, 'locale':code, 'status':'needs_review', 'score':.8,
            'draft_text':POST[code], 'draft_meta':{'x_length':80, 'review':[t(code, 'images_review', count=1)],
                                                'warnings':[t(code, 'clean_failed')]}}
    app.repo.returns.update(get_task=task, list_media=[
        {'idx':0, 'decision':'recreate', 'asset_key':'a.png', 'asset_kind':'final', 'ai_generated':1},
        {'idx':1, 'decision':'review', 'decision_reason':t(code, 'image_failed')}])
    app.hub.r2.returns['get_object'] = png_bytes()
    b = bot(app)
    await b.send_draft('t1')
    assert len(sent_files(b.client.named('send_file')[0][0][1])) == 1
    messages = b.client.named('send_message')
    assert b.client.named('send_file')[0][1]['caption'] == POST[code]
    assert len(messages) == 1
    info, kwargs = messages[0]
    assert not wrong_language(info[1], code)
    texts = [button.text for row in kwargs['buttons'] for button in row]
    assert t(code, 'btn_regenerate') in texts and t(code, 'btn_approve_reviewed') in texts
    assert t(code, 'btn_publish') in texts and t(code, 'btn_not_publish') in texts
    if code == 'zh-CN':
        assert '需要审核' in info[1] and '重绘' in info[1]
        assert not any(word in info[1] for word in ('review', 'recreate', 'Draft', 'QC'))


async def test_bot_language_switch_updates_existing_shared_config(app, settings):
    service = ConfigService(settings, app.repo)
    app.config = service
    app.ops = Ops(app)
    b = bot(app)
    for code in ('zh-CN', 'en-US'):
        e = SimpleNamespace(sender_id=1, data=f'm:locale:{code}'.encode(),
                            answer=Recorder().answer, respond=Recorder().respond)
        await b._on_callback(e)
        assert app.config.current.default_locale == code
        assert app.repo.named('set_settings')[-1][0][0] == {'default_locale':code}


@pytest.mark.parametrize('code', LOCALES)
async def test_regeneration_adopts_selected_locale(app, code):
    app.config.current.default_locale = code
    app.repo.returns['get_task'] = {'id':'t1', 'status':'draft_ready', 'locale':'en-US'}
    app.storage.returns['release_task'] = 0
    app.workers = Recorder(enqueue=True)
    ok, reason = await Ops(app).regenerate('t1')
    assert ok and reason == t(code, 'ops_queued')
    assert app.repo.named('set_task_locale')[0][0] == ('t1', code, 'US')


@pytest.fixture
def admin_app(app, settings):
    web = FastAPI()
    web.include_router(router)
    web.dependency_overrides[require_admin] = lambda: 'test-admin'
    web.state.ctx = app
    app.config = ConfigService(settings, app.repo)
    app.repo.returns.update(status_counts={'received':1, 'needs_review':1}, stored_bytes=0,
        list_tasks=[{'id':'t1','status':'needs_review','stage':'rewrite','score':.8,
                     'preview':'','error':'','updated_at':''}], list_models=[])
    app.workers = SimpleNamespace(stats={'queued':1,'busy':[],'workers':3})
    task = {'id':'t1','status':'needs_review','stage':'rewrite','attempts':1,'score':.8,
            'route':'review','locale':'zh-CN','market':'US','created_at':'','draft_text':POST['zh-CN'],
            'draft_meta':{'review':['需要核实来源。']}, 'envelope':{}, 'source_text':'原始消息', 'jev_result':None, 'evaluation':None}
    app.repo.returns.update(get_task=task, list_events=[])
    app.ops = Ops(app)
    return web


@pytest.mark.parametrize('code', LOCALES)
async def test_admin_switch_render_pages_and_validation_are_localized(admin_app, app, code):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url='http://test') as client:
        switched = await client.post('/settings/locale', data={'locale':code})
        assert switched.status_code == 303 and app.config.current.default_locale == code
        for path in ('/', '/settings', '/tasks/t1'):
            response = await client.get(path)
            assert response.status_code == 200
            assert f'<html lang="{code}">' in response.text
            assert 'admin_' not in response.text
            assert t(code, 'admin_Settings') in response.text
            if code == 'zh-CN':
                for old_label in ('Runtime settings','Test connections','Refresh models','Blocking problems','Needs review','busy workers'):
                    assert old_label not in response.text
        bad = await client.post('/settings/locale', data={'locale':'xx-XX'})
        assert bad.status_code == 303 and app.config.current.default_locale == code
        assert 'password' not in (await client.get('/settings')).text.lower()
        # Configuration validation also rejects unsupported language through the existing form.
        invalid = await client.post('/settings', data={'default_locale':'xx-XX'})
        assert invalid.status_code == 303 and app.config.current.default_locale == code


@pytest.mark.parametrize('code', LOCALES)
@pytest.mark.parametrize('image_passes', [True, False])
async def test_full_pipeline_clean_jev_localized_draft_image_qc_and_storage(app, code, image_passes):
    app.config.current.default_locale = code
    original = env(code, message_texts=['Forwarded from Noisy Channel\n' + SOURCE + '\nJoin our channel'],
                   media=[SourceMedia(message_id=10, kind=MediaKind.PHOTO)],
                   forwards=[ForwardOrigin(chat_id=-10099, sender_name='Noisy Channel')])
    task = {'id':'t1', 'status':'received', 'attempts':0, 'envelope':original.model_dump(mode='json')}
    app.repo.returns.update(get_task=task, claim_task=True, get_rules={}, get_hooks=[])
    app.telegram = Recorder(fetch_media={10:png_bytes()})
    app.hub.jev.returns['ask'] = resp(3, .9, media=.9)
    def chat(model, messages, schema, **kwargs):
        if schema is CoreContent:
            return CoreContent(core_text=SOURCE)
        if schema is LanguageCheck:
            return LanguageCheck(passed=True)
        if schema is ImageAnalysis:
            return ImageAnalysis(description='设备信息图' if code == 'zh-CN' else 'Device infographic',
                                 image_type='infographic', has_channel_overlay=True, relevance=.9,
                                 extracted_text='12 devices', source_facts=['支持 12 台设备。'] if code == 'zh-CN'
                                 else ['Supports 12 devices.'])
        if schema is VerifiedFacts:
            return VerifiedFacts(facts=[VerifiedFact(text=SOURCE if code == 'en-US' else '新功能支持 12 台设备。', evidence=SOURCE)])
        if schema is FactVerification:
            return FactVerification(passed=True)
        if schema is Evaluation:
            return Evaluation(suitable=True, value_score=.8, reason='具有实用价值。' if code == 'zh-CN'
                              else 'Useful compatibility facts.')
        if schema is RewriteResult:
            return RewriteResult(post=POST[code], hook='核对兼容性' if code == 'zh-CN' else 'Check compatibility',
                                 added_value='提醒核对适配范围。' if code == 'zh-CN' else 'A practical check.',
                                 image_brief='非纪实设备信息卡片。' if code == 'zh-CN' else 'An original device information card.')
        if schema is ImageQC:
            return good_qc(passed=image_passes, numbers_consistent=image_passes, rendered_text='12')
        raise AssertionError(schema)
    app.hub.cpa.returns.update(chat_json=chat, images_generate=png_bytes())
    app.storage.returns['persist'] = 'assets/final.png'
    await Pipeline(app).process('t1')
    state = app.hub.jev.named('ask')[0][0][0]
    assert state['post_text'] == SOURCE
    assert 'Noisy Channel' not in json.dumps(state)
    _, status, draft, meta = app.repo.named('save_draft')[0][0]
    assert draft == POST[code]
    assert all(not wrong_language(message, code) for message in meta['review'] + meta['problems'])
    assert status.value == ('draft_ready' if image_passes else 'needs_review')
    assert bool(app.storage.named('persist')) is image_passes
    assert app.telegram.named('send_draft')[0][0] == ('t1',)
    steps = [args[1] for args, _ in app.repo.named('set_stage')]
    assert steps.index('clean') < steps.index('triage') < steps.index('media')
    assert original.text == SOURCE and original.message_texts[0].startswith('Forwarded from')


@pytest.mark.parametrize('code', LOCALES)
async def test_wrong_language_evaluation_retries_before_skip_reason(app, code):
    bad = Evaluation(suitable=False, value_score=.1, reason='Not useful' if code == 'zh-CN' else '没有价值')
    good = bad.model_copy(update={'reason':'没有实用价值。' if code == 'zh-CN' else 'No practical value.'})
    evaluations = iter([bad, good])
    checks = iter([LanguageCheck(passed=False), LanguageCheck(passed=True)])
    def chat(model, messages, schema, **kwargs):
        return next(evaluations) if schema is Evaluation else next(checks)
    app.hub.cpa.returns['chat_json'] = chat
    from tg_x_copilot.models import TriageResult
    result = await Pipeline(app)._evaluate(env(code), TriageResult(route='proceed', value=.8), {},
                                          app.i18n.get(code), app.config.current)
    assert result.reason == good.reason


async def test_hidden_source_links_survive_cleaning_without_tracking(app):
    original = env(message_texts=[SOURCE], message_urls=[['https://example.org/source?utm_medium=tg&id=12']])
    def extract(model, messages, schema, **kwargs):
        assert 'https://example.org/source?id=12' in messages[-1]['content']
        assert 'utm_medium' not in messages[-1]['content']
        return CoreContent(core_text=SOURCE + '\nhttps://example.org/source?id=12')
    app.hub.cpa.returns['chat_json'] = extract
    canonical, _ = await clean_bundle(original, app)
    assert canonical.urls == ['https://example.org/source?id=12']


async def test_promotion_only_message_cleans_to_empty_without_persistence(app):
    original = env(message_texts=['关注本频道 https://t.me/channel\nContact: @sales'])
    canonical, fallback = await clean_bundle(original, app)
    assert not canonical.text and not canonical.urls and not fallback
    assert not app.hub.cpa.named('chat_json') and not app.storage.named('persist')


@pytest.mark.parametrize('code', LOCALES)
async def test_failed_notification_hides_errors_and_uses_task_language(app, code):
    app.repo.returns['get_task'] = {'locale':code, 'tg_chat_id':5}
    app.telegram = Recorder()
    await Pipeline(app).mark_failed('t1', RuntimeError('upstream error with private configuration'))
    message = app.telegram.named('update_task_status')[-1][1]['text']
    assert t(code, 'task_failure') in message
    assert 'private configuration' not in message
    assert not wrong_language(message, code)


@pytest.mark.parametrize('code', LOCALES)
async def test_health_page_status_and_detail_are_localized(admin_app, app, code):
    from tg_x_copilot.services.ops import CheckResult
    app.config.current.default_locale = code
    app.ops = Recorder(test_connections=[CheckResult('mysql', True, 2, 'SELECT 1 ok'),
                                         CheckResult('cpa', False, 3, 'upstream error')])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url='http://test') as client:
        response = await client.get('/actions/health')
    assert response.status_code == 200
    assert t(code, 'connection_ok') in response.text and t(code, 'connection_failed') in response.text
    assert 'upstream error' not in response.text and 'SELECT 1 ok' not in response.text


@pytest.mark.parametrize('code', LOCALES)
async def test_every_image_edit_output_requires_qc_before_final(app, code):
    app.config.current.pipeline.direct_uploads_owned = True
    analysis = ImageAnalysis(description='photo', image_type='photo_real_event', quality='low', relevance=.9)
    image = LoadedImage(0, inspect_image(png_bytes()), SourceMedia(message_id=10, kind=MediaKind.PHOTO))
    app.hub.cpa.returns.update(images_edit=png_bytes(), chat_json=good_qc(people_consistent=False))
    [result] = await Pipeline(app)._process_images('t1', env(code), [image], {0:analysis},
        RewriteResult(post=POST[code], hook='hook'), Evaluation(suitable=True, value_score=.8),
        app.i18n.get(code), app.config.current,
        verified_facts=VerifiedFacts(facts=[VerifiedFact(text=SOURCE, evidence=SOURCE)]))
    assert result.decision is ImageDecision.REVIEW and result.output is None
    assert result.review_blob is not None


def test_unknown_forward_origin_never_inherits_confirmed_rights():
    from tg_x_copilot.pipeline.image_policy import media_is_owned
    legacy = env(forwards=[ForwardOrigin(chat_id=-1001), ForwardOrigin(sender_name='Unknown')])
    media = SourceMedia(message_id=10, kind=MediaKind.PHOTO)
    assert not media_is_owned(media, legacy, owned_ids={-1001}, direct_uploads_owned=True)
    authorized = media.model_copy(update={'forwarded':True, 'source_chat_id':-1001})
    unknown = media.model_copy(update={'forwarded':True, 'source_chat_id':None})
    direct = media.model_copy(update={'forwarded':False})
    assert media_is_owned(authorized, legacy, owned_ids={-1001}, direct_uploads_owned=False)
    assert not media_is_owned(unknown, legacy, owned_ids={-1001}, direct_uploads_owned=True)
    assert not media_is_owned(direct, legacy, owned_ids={-1001}, direct_uploads_owned=False)


def test_traditional_or_unknown_chinese_image_text_is_localized():
    for script in ('traditional', 'mixed', None):
        analysis = ImageAnalysis(description='说明图', image_type='infographic', relevance=.9,
                                 contains_text=True, text_language='zh', text_script=script)
        assert decide(analysis, owned_source=True, target_language='zh', locale='zh-CN')[0] is ImageDecision.LOCALIZE
    analysis.text_script = 'simplified'
    assert decide(analysis, owned_source=True, target_language='zh', locale='zh-CN')[0] is ImageDecision.KEEP


@pytest.mark.parametrize('code', LOCALES)
async def test_language_retries_exhausted_never_saves_mixed_draft(app, code):
    app.hub.cpa.returns['chat_json'] = RewriteResult(
        post=POST['en-US' if code == 'zh-CN' else 'zh-CN'], hook='hook', added_value='context')
    with pytest.raises(ValueError, match='no localized draft'):
        await Pipeline(app)._rewrite('t1', env(code), Evaluation(suitable=True, value_score=.8), {},
                                      XRules(), [], app.i18n.get(code), app.config.current)
    assert not app.repo.named('save_draft') and not app.storage.named('persist')


async def test_telegram_all_final_assets_are_sent_in_batches(app):
    app.repo.returns.update(get_task={'id':'t1','locale':'en-US','tg_chat_id':5,'status':'draft_ready',
                                    'score':.8,'draft_text':POST['en-US'],'draft_meta':{}},
        list_media=[{'idx':idx,'asset_key':f'assets/{idx}.png','asset_kind':'final','decision':'keep'}
                    for idx in range(11)])
    app.hub.r2.returns['get_object'] = png_bytes()
    b = bot(app)
    await b.send_draft('t1')
    assert [len(sent_files(args[1])) for args, _ in b.client.named('send_file')] == [10, 1]
