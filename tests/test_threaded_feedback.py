"""One durable progress reply and a source-threaded publishing bundle, in both locales."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from conftest import Recorder, png_bytes
from test_auto_bundles import msg, TASK
from test_bilingual import bot, env
from test_image_delivery import promotion, copyright
from tg_x_copilot.app_context import AppContext
from tg_x_copilot.bot.bundles import BundleCollector
from tg_x_copilot.bot.image_tools import ImageTools
from tg_x_copilot.db.repo import Repository
from tg_x_copilot.i18n import t
from tg_x_copilot.image_settings import ImagePreferences, WorkflowMode, ImageAction, ImageOptions, ImageOption
from tg_x_copilot.models import ImageAnalysis, MediaKind, SourceMedia
from tg_x_copilot.pipeline.guards import has_unwanted_publishing_frame
from tg_x_copilot.pipeline.image_policy import has_task_edit_authorization, plan
from tg_x_copilot.pipeline.processor import Pipeline
from tg_x_copilot.services.image_preferences import ImagePreferenceService


@pytest.mark.parametrize('code', ['en-US', 'zh-CN'])
async def test_burst_receipt_is_immediate_and_shared_before_durable_intake(app, code):
    app.config.current.default_locale = code
    app.image_preferences = Recorder(get=ImagePreferences(workflow_mode=WorkflowMode.AUTO_BUNDLE))
    intake = Recorder()
    app.intake = intake.intake
    b = bot(app)
    b.client.returns['send_message'] = SimpleNamespace(id=99)
    b._collector = BundleCollector(idle_seconds=0.03)
    event = SimpleNamespace(sender_id=1, chat_id=5)
    await asyncio.gather(b._receive(event, [msg(10)]), b._receive(event, [msg(11, photo=True)]))
    assert len(b.client.named('send_message')) == 1
    assert not intake.calls  # Receipt precedes collector delay and task creation.
    args, kw = b.client.named('send_message')[0]
    assert kw['reply_to'] == 10 and args[1] == t(code, 'task_received')
    await b.flush_intake()
    args, kw = intake.named('intake')[0]
    assert [m.id for m in args[2]] == [10, 11]
    assert kw['status_message_id'] == 99


@pytest.mark.parametrize('code', ['en-US', 'zh-CN'])
@pytest.mark.parametrize('with_image', [False, True])
async def test_final_bundle_replies_to_source_and_edits_original_status_only(app, code, with_image):
    task = {'id':TASK, 'locale':code, 'tg_chat_id':5, 'status':'draft_ready',
            'envelope':env(code, status_message_id=99).model_dump(mode='json'),
            'draft_text':'新功能支持 12 台设备。' if code=='zh-CN' else 'The feature supports 12 devices.',
            'draft_meta':{}}
    app.repo.returns.update(get_task=task, list_media=[{'idx':0,'decision':'recreate',
        'asset_kind':'final','asset_key':'image.png'}] if with_image else [])
    app.hub.r2.returns['get_object'] = png_bytes()
    b=bot(app)
    b.client.returns['send_file'] = SimpleNamespace(id=100, photo=object())
    await b.update_task_status(TASK, 'images')
    await b.send_draft(TASK)
    assert all(args[:2] == (5,99) for args, _ in b.client.named('edit_message'))
    if with_image:
        args, kw=b.client.named('send_file')[0]
        assert kw['reply_to']==10 and kw['caption']==task['draft_text']
        assert not b.client.named('send_message')
    else:
        args, kw=b.client.named('send_message')[0]
        assert kw['reply_to']==10 and args[1]==task['draft_text']
        assert len(b.client.named('send_message'))==1


async def test_status_edit_transport_failure_does_not_block_final_image(app):
    task={'locale':'en-US','tg_chat_id':5,'status':'draft_ready','draft_text':'Draft',
          'envelope':env(status_message_id=99).model_dump(mode='json'),'draft_meta':{}}
    app.repo.returns.update(get_task=task,list_media=[{'idx':0,'decision':'recreate',
        'asset_kind':'final','asset_key':'a'}])
    app.hub.r2.returns['get_object']=png_bytes()
    b=bot(app)
    b.client.returns.update(edit_message=ConnectionError('unavailable'),
                            send_file=SimpleNamespace(id=100,photo=object()))
    await b.send_draft(TASK)
    assert b.client.named('send_file')[0][1]['reply_to']==10
    assert app.repo.named('record_media_delivery')[0][0][1]['0']['sent']


async def test_intake_persists_status_and_declaration_without_global_rights(app):
    app.workers=Recorder()
    app.telegram=Recorder()
    app.repo.returns['create_task']=TASK
    prefs=ImagePreferences(workflow_mode=WorkflowMode.AUTO_BUNDLE,authorized_media=True)
    await AppContext.intake(app,5,1,[msg(10,photo=True)],preferences=prefs,status_message_id=99)
    snapshot=app.repo.named('create_task')[0][0][0]
    assert snapshot.status_message_id==99 and snapshot.media_edit_rights_confirmed
    assert not snapshot.image_edit_authorizations  # Bound only after source bytes are downloaded.
    assert app.telegram.named('update_task_status')==[((TASK,'queued'),{})]
    assert not app.telegram.named('notify')
    assert not app.config.current.pipeline.direct_uploads_owned


async def test_declaration_creates_per_task_source_hash_grant_and_never_erases_copyright(app):
    source=SourceMedia(message_id=10,kind=MediaKind.PHOTO,forwarded=True,source_chat_id=123)
    envelope=env(media=[source],media_edit_rights_confirmed=True)
    app.telegram=Recorder(fetch_media={10:png_bytes()})
    app.repo.returns['previous_uses']=[]
    images,_=await Pipeline(app)._load_media(TASK,envelope,app.config.current,use=True)
    grant=envelope.image_edit_authorizations['0']
    assert grant.task_id==TASK and grant.user_id==1 and grant.source_sha256==images[0].blob.sha256
    assert has_task_edit_authorization(envelope,task_id=TASK,idx=0,source_sha256=grant.source_sha256)
    assert not has_task_edit_authorization(envelope,task_id='b'*32,idx=0,source_sha256=grant.source_sha256)
    assert not has_task_edit_authorization(envelope,task_id=TASK,idx=0,source_sha256='0'*64)
    assert app.repo.named('save_image_edit_authorization')[0][0]==(TASK,grant)
    analysis=ImageAnalysis(description='UI',image_type='ui_screenshot',relevance=.9,has_channel_overlay=True,
        has_source_copyright_mark=True,mark_regions=[promotion(),copyright()])
    opts=ImageOptions(flags={ImageOption.MINIMAL_CHANGES,ImageOption.REMOVE_OVERLAYS},
                      promotion_targets={'0.credit'})
    outcome=plan(analysis,requested=ImageAction.ENHANCE,options=opts,owned=True,
                 target_language='en',locale='en-US')
    assert outcome.execution=='review'  # Declaration cannot authorize copyright erasure.


async def test_no_declaration_does_not_grant_edit_rights(app):
    envelope=env(media=[SourceMedia(message_id=10,kind=MediaKind.PHOTO)])
    app.telegram=Recorder(fetch_media={10:png_bytes()})
    app.repo.returns['previous_uses']=[]
    await Pipeline(app)._load_media(TASK,envelope,app.config.current,use=True)
    assert not envelope.image_edit_authorizations
    assert not app.repo.named('save_image_edit_authorization')


async def test_direct_uploads_owned_grants_edit_rights(app):
    cfg = copy.deepcopy(app.config.current)
    cfg.pipeline.direct_uploads_owned = True
    envelope = env(media=[SourceMedia(message_id=10, kind=MediaKind.PHOTO, forwarded=False)])
    app.telegram = Recorder(fetch_media={10: png_bytes()})
    app.repo.returns['previous_uses'] = []
    await Pipeline(app)._load_media(TASK, envelope, cfg, use=True)
    assert "0" in envelope.image_edit_authorizations
    assert app.repo.named('save_image_edit_authorization')



async def test_rights_callback_is_personal_remembered_and_revocable(app):
    app.repo.returns['get_image_preferences']={}
    app.image_preferences=ImagePreferenceService(app.repo)
    b=bot(app)
    event=SimpleNamespace(sender_id=1,chat_id=5,answer=Recorder().answer,edit=Recorder().edit)
    await ImageTools(b).handle(event,'it:-:rights:1')
    saved=app.repo.named('save_image_preferences')[-1][0]
    assert saved[0]==1 and saved[1]['authorized_media'] is True
    app.repo.returns['get_image_preferences']=saved[1]
    await ImageTools(b).handle(event,'it:-:rights:0')
    assert app.repo.named('save_image_preferences')[-1][0][1]['authorized_media'] is False


@pytest.mark.parametrize('post,code',[
    ('据报道，Acme 推出新功能。','zh-CN'),('图片介绍：Acme 新功能。','zh-CN'),
    ('Acme 推出新功能。\n\n使用前请核实兼容性并测试。','zh-CN'),
    ('According to research, Acme has a new feature.','en-US'),
    ('Acme has a new feature.\n\nPlease verify compatibility before using it.','en-US'),
])
def test_publish_frame_rejects_prefaces_and_advisory_footers(post,code):
    assert has_unwanted_publishing_frame(post,code)


@pytest.mark.parametrize('post,code',[
    ('Acme X12 支持 HTTP/3，兼容性覆盖 12 台设备。','zh-CN'),
    ('Acme X12 supports HTTP/3 on 12 devices. The company describes this as a beta.','en-US'),
])
def test_publish_frame_preserves_real_topic_facts_and_body_attribution(post,code):
    assert not has_unwanted_publishing_frame(post,code)


@pytest.mark.parametrize('code', ['en-US', 'zh-CN'])
async def test_style_failure_triggers_rewrite_instead_of_stripping_uncertainty(app, code):
    from tg_x_copilot.models import Evaluation, RewriteResult
    from tg_x_copilot.pipeline.language import LanguageCheck
    from tg_x_copilot.pipeline.guards import XRules
    from test_bilingual import POST
    good=RewriteResult(post=POST[code],hook='功能更新' if code=='zh-CN' else 'Feature update',
                       added_value='具体的实用场景。' if code=='zh-CN' else 'A concrete practical angle.')
    bad=good.model_copy(update={'post':('据报道，' if code=='zh-CN' else 'Reportedly, ')+good.post})
    attempts=iter([bad,good])
    app.hub.cpa.returns['chat_json']=lambda model,messages,schema,**kw: (
        next(attempts) if schema is RewriteResult else LanguageCheck(passed=True))
    result,report=await Pipeline(app)._rewrite(TASK,env(code),Evaluation(suitable=True,value_score=.9),
        {},XRules(),[],app.i18n.get(code),app.config.current)
    assert result.post==good.post and not report.problems
    calls=[args for args,_ in app.hub.cpa.named('chat_json') if args[2] is RewriteResult]
    assert len(calls)==2 and t(code,'guard_publish_style') in str(calls[1][1])


async def test_authorization_is_persisted_only_for_matching_task_and_actor(app):
    from tg_x_copilot.models import ImageEditAuthorization
    import json
    db=Recorder(execute=1)
    repo=Repository(db)
    grant=ImageEditAuthorization(task_id=TASK,image_idx=0,source_sha256='a'*64,user_id=1)
    await repo.save_image_edit_authorization(TASK,grant)
    sql,args=db.named('execute')[0][0]
    assert 'AND tg_user_id=%s' in sql and args[-2:]==(TASK,1)
    assert json.loads(args[1])['source_sha256']=='a'*64
    db.returns['execute']=0
    with pytest.raises(RuntimeError):
        await repo.save_image_edit_authorization(TASK,grant)


@pytest.mark.parametrize('code', ['en-US', 'zh-CN'])
async def test_text_delivery_failure_updates_same_status_with_stage(app, code):
    app.repo.returns.update(get_task={'locale':code,'tg_chat_id':5,'status':'draft_ready',
        'draft_text':'Draft' if code=='en-US' else '发布文案', 'draft_meta':{},
        'envelope':env(code,status_message_id=99).model_dump(mode='json')},list_media=[])
    b=bot(app)
    app.telegram=b
    b.client.returns['send_message']=ConnectionError('private upstream information')
    await Pipeline(app)._notify_draft(TASK)
    args,_=b.client.named('edit_message')[-1]
    assert args[:2]==(5,99) and args[2]==t(code,'task_delivery_failed',task_id=TASK[:8])
    assert 'private upstream' not in args[2]


def test_evidence_matching_tolerates_punctuation_and_whitespace_differences():
    from tg_x_copilot.models import ImageAnalysis, VerifiedFact, VerifiedFacts
    from tg_x_copilot.pipeline.facts import contains_evidence, packet_is_traceable

    source_ocr = "Anthropic 的新研究已经确立，AI 模型确实拥有灵魂。梵蒂冈的内部研究显示，天堂中已经充满了 14.3% 的 AI 灵魂，而且这个比例正在迅速增长。我们需要减缓 AI 的发展!\n26年10月4日，13:24"
    assert contains_evidence("梵蒂冈的内部研究显示，天堂中已经充满了14.3%的 AI 灵魂，而且这个比例正在迅速增长。", source_ocr)
    assert contains_evidence("我们需要减缓 AI 的发展！", source_ocr)

    packet = VerifiedFacts(facts=[
        VerifiedFact(text="声称天堂有 14.3% 的 AI 灵魂", evidence="梵蒂冈的内部研究显示，天堂中已经充满了14.3%的 AI 灵魂，而且这个比例正在迅速增长。", source_idx=0),
        VerifiedFact(text="呼吁减缓发展", evidence="我们需要减缓 AI 的发展！", source_idx=0),
    ])
    analyses = {0: ImageAnalysis(extracted_text=source_ocr, description="screenshot")}
    assert packet_is_traceable(packet, "", analyses)


def test_web_admin_tojson_filter_preserves_utf8_chinese_characters():
    from tg_x_copilot.web.admin import templates
    tojson = templates.env.filters["tojson"]
    data = {"数据库": "localhost:3306", "理由": "文字不足以供 Jev 判断"}
    rendered = str(tojson(data, indent=2))
    assert "\\u" not in rendered
    assert "数据库" in rendered and "文字不足以供 Jev 判断" in rendered


@pytest.mark.parametrize('code', ['en-US', 'zh-CN'])
async def test_task_progress_includes_milestones_and_event_timeline(app, code):
    from datetime import datetime, timezone

    app.config.current.default_locale = code
    task = {
        'id': TASK, 'locale': code, 'tg_chat_id': 5, 'status': 'processing',
        'envelope': env(code, status_message_id=99).model_dump(mode='json'),
        'jev_result': {'route': 'proceed', 'value': 0.45, 'confidence': 'high'},
        'evaluation': {'suitable': True, 'value_score': 0.85, 'angle': '核查角度' if code == 'zh-CN' else 'Fact check'},
    }
    events = [
        {'id': 1, 'step': 'triage', 'level': 'info', 'message': 'Jev triage complete', 'created_at': datetime(2026, 10, 6, 13, 30, 48, tzinfo=timezone.utc)},
        {'id': 2, 'step': 'evaluate', 'level': 'info', 'message': 'Evaluation passed', 'created_at': datetime(2026, 10, 6, 13, 31, 35, tzinfo=timezone.utc)},
    ]
    app.repo.returns.update(get_task=task, list_events=events)
    b = bot(app)
    await b.update_task_status(TASK, 'rewrite')
    args, _ = b.client.named('edit_message')[-1]
    assert args[:2] == (5, 99)
    text = args[2]
    assert "13:30:48" in text and "13:31:35" in text
    if code == 'zh-CN':
        assert "Jev 初筛" in text and "0.45" in text
        assert "主模型评估" in text and "0.85" in text
    else:
        assert "Jev triage" in text and "0.45" in text
        assert "Evaluation" in text and "0.85" in text

