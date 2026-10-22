import pytest
from conftest import Recorder
from pydantic import ValidationError

from tg_x_copilot.config import AppSettings
from tg_x_copilot.services.config_service import ConfigService


async def test_runtime_settings_validate_persist_and_parse_string_and_integer_lists():
    repo = Recorder()
    service = ConfigService(AppSettings(_env_file=None), repo)
    assert service.parse_value('web.fetch_strategies', 'reader, browser, direct', []) == ['reader', 'browser', 'direct']
    assert service.parse_value('pipeline.owned_source_ids', '-1001, -1002', []) == [-1001, -1002]
    await service.update({'mobile_text.engine': 'typeset', 'mobile_text.style': 'clean',
                          'mobile_text.content_max_chars': '600', 'pipeline.quick_image_attempts': '2'})
    assert service.current.mobile_text.content_max_chars == 600
    assert service.current.mobile_text.engine == 'typeset'
    persisted = len(repo.named('set_settings'))
    for changes in ({'mobile_text.content_max_chars': 10}, {'mobile_text.image_size': '1024x1024'},
                    {'mobile_text.style': 'unknown'}, {'pipeline.quick_image_attempts': 0}):
        with pytest.raises(ValidationError):
            await service.update(changes)
    assert len(repo.named('set_settings')) == persisted
    rows = {row['key']: row for row in service.view()}
    assert rows['mobile_text.engine']['choices'] == ['model', 'typeset']
    assert rows['mobile_text.content_max_chars']['bounds'] == {'min': 200, 'max': 1800}


def test_web_settings_render_choices_and_save_all_fields_without_breaking_reader_list():
    from test_quick_and_links import make_client

    client, _ = make_client()
    ctx = client.app.state.ctx
    ctx.repo.returns['list_models'] = []
    ctx.config = ConfigService(ctx.config.base, ctx.repo)
    page = client.get('/settings')
    assert page.status_code == 200
    assert 'Whiteboard hand-drawn' in page.text
    assert 'name="mobile_text.engine"' in page.text
    assert 'name="mobile_text.style_instructions"' in page.text
    data = {}
    for row in ctx.config.view():
        if row['is_bool']:
            data[row['key'] + '__bool'] = '1'
            if row['value'] == 'True':
                data[row['key']] = 'true'
        else:
            data[row['key']] = row['value']
    data.update({'mobile_text.engine': 'typeset', 'mobile_text.style': 'clean',
                 'mobile_text.content_max_chars': '600', 'web.fetch_strategies': 'reader, browser, direct'})
    response = client.post('/settings', data=data, follow_redirects=False)
    assert response.status_code == 303
    assert ctx.config.current.mobile_text.engine == 'typeset'
    assert ctx.config.current.mobile_text.content_max_chars == 600
    assert ctx.config.current.web.fetch_strategies == ['reader', 'browser', 'direct']
    assert ctx.config.current.pipeline.owned_source_ids == []
    assert ctx.config.current.mobile_text.fallback_to_typeset
