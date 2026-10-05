from tg_x_copilot import prompts
from tg_x_copilot.clients.openai_compat import extract_json
from tg_x_copilot.config import AppSettings, apply_overrides
from tg_x_copilot.i18n import I18n


def test_overrides_apply_whitelisted_only():
    base = AppSettings(_env_file=None)
    s = apply_overrides(base, {"models.text_model": "foo", "db.host": "evil",
                               "pipeline.owned_source_ids": [-1001]})
    assert s.models.text_model == "foo"
    assert s.db.host == base.db.host
    assert s.pipeline.owned_source_ids == [-1001]


def test_jev_defaults_point_at_typesafe_and_key_is_secret():
    s = apply_overrides(AppSettings(_env_file=None), {"jev.api_key": "ts-secret-key-123"})
    assert s.jev.base_url == "https://api.typesafe.ai/v1" and s.jev.model == "jev-latest"
    assert "ts-secret-key-123" not in repr(s.jev)
    assert s.jev.api_key.get_secret_value() == "ts-secret-key-123"


def test_prompts_render_and_fallback():
    p = prompts.render("rewrite", "de-DE", market="US", language_name="English (US)",
                       max_chars=280)
    assert "280" in p.system and "$max_chars" not in p.system
    assert p.user


def test_i18n_fallback():
    i = I18n()
    assert i.get("xx-XX").code == "en-US"
    assert "abc" in i.t("xx-XX", "queued", count=1, task_id="abc")


def test_extract_json_tolerates_fences():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": 2} hope that helps') == {"a": 2}
