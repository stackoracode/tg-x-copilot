"""Regression: a successfully fetched Chinese article must survive URL cleaning."""

from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from tg_x_copilot.models import InputEnvelope
from tg_x_copilot.pipeline.cleaning import CoreContent, clean_bundle, clean_url, preclean
from tg_x_copilot.pipeline.links import attach_linked_pages, links_to_read
from tg_x_copilot.pipeline.web_input import build_web_envelope, split_urls
from tg_x_copilot.services.link_reader import LinkReader, ReadResult
from tg_x_copilot.services.url_fetch import PageContent, UrlFetchError, check_url
from tg_x_copilot.urls import extract_urls

ARTICLE = (
    "本指南在指南注册平台（https://www.guidelines-registry.org）注册并上传计划书"
    "（注册号：PREPARE-2025CN1551），设计与制订参照指南科学性（Scientific）、"
    "透明性（Transparent）和适用性（Applicable）的评级（Rankings）（简称“STAR”）"
    "推荐制修订决策框架进行。使用推荐意见分级的评估、制订及评价（Grading of Recommendations）。"
)
REGISTRY = "https://www.guidelines-registry.org"


def test_reported_article_url_stops_at_chinese_bracket_without_losing_prose():
    assert preclean(ARTICLE) == ARTICLE
    assert extract_urls(ARTICLE) == [REGISTRY]
    assert split_urls(ARTICLE) == [REGISTRY]
    env = build_web_envelope(text=ARTICLE, urls=[], pages=[], images=[], own_content=False,
                             locale="zh-CN", market="CN")
    assert env.text == ARTICLE and env.urls == [REGISTRY]


@pytest.mark.parametrize(("text", "url"), [
    ("地址：https://example.org/a，下一句。", "https://example.org/a"),
    ("参见《https://example.org/a》及后文。", "https://example.org/a"),
    ("链接“https://example.org/a”；正文", "https://example.org/a"),
    ("Read (https://example.org/a).", "https://example.org/a"),
    ("[reference](https://example.org/a)", "https://example.org/a"),
    ("See https://example.org/wiki/Topic_(details).", "https://example.org/wiki/Topic_(details)"),
    ("中文路径 https://example.org/研究/计划?名称=指南。", "https://example.org/研究/计划?名称=指南"),
    ("IP https://[2606:4700:4700::1111]/dns-query", "https://[2606:4700:4700::1111]/dns-query"),
    ("URL HTTPS://example.org/a", "HTTPS://example.org/a"),
])
def test_prose_boundaries_preserve_real_url_syntax(text, url):
    assert extract_urls(text) == [url]
    assert unquote(preclean(text)) == text.replace("HTTPS://", "https://")


def test_tracking_cleanup_preserves_article_punctuation_and_balanced_parentheses():
    source = "参考（https://example.org/wiki/Test_(a)?utm_source=tg&id=9），注册号：123。"
    assert preclean(source) == "参考（https://example.org/wiki/Test_(a)?id=9），注册号：123。"
    assert preclean("See (https://t.me/spam). Keep the report.") == "See (). Keep the report."
    assert split_urls("https://a.example,https://b.example?q=a,b") == [
        "https://a.example", "https://b.example?q=a,b"]


@pytest.mark.parametrize("url", [
    "https://example.org：443/path",  # NFKC would turn the fullwidth colon into a delimiter
    "https://[broken/path", "https://example.org:bad/path", "https://example.org:99999/path",
])
async def test_malformed_urls_are_controlled_failures_and_never_reach_network(url, app):
    calls = []

    async def resolve(host, port):
        calls.append((host, port))
        return ["93.184.216.34"]

    assert clean_url(url) == ""
    with pytest.raises(UrlFetchError, match="invalid URL syntax"):
        await check_url(url, resolve)
    result = await LinkReader(app, resolver=resolve).read(url)
    assert result.page is None and result.attempts[0].method == "check"
    assert not calls and not app.hub.cpa.calls
    env = InputEnvelope(chat_id=1, user_id=1, message_ids=[1], urls=[url], fetch_links=True)
    assert links_to_read(env, app.config.current.web) == []


def test_malformed_reference_remains_source_text_without_becoming_a_fetch_target():
    text = "Original reference http://[broken and registration 123."
    assert preclean(text) == text
    assert extract_urls(text) == []


@pytest.mark.parametrize("direct", [False, True])
async def test_successful_page_fetch_then_cleaning_handles_reported_article(app, direct):
    url = "https://mp.weixin.qq.com/s/article"
    app.links = SimpleNamespace(read=lambda *args, **kwargs: None)

    async def read(link, *, allow_ai):
        return ReadResult(link, PageContent(link, "指南制订", ARTICLE))

    app.links.read = read
    def extract(model, messages, schema, **kwargs):
        return CoreContent(core_text=ARTICLE if "PREPARE" in messages[-1]["content"] else "")

    app.hub.cpa.returns["chat_json"] = extract
    env = InputEnvelope(channel="web", chat_id=0, user_id=0, message_ids=[1], text=url,
                        message_texts=[url], urls=[url], fetch_links=True, is_direct=direct,
                        quick=True, locale="zh-CN")

    async def event(*args, **kwargs):
        pass

    fetched, _, _ = await attach_linked_pages("task", env, app, event)
    cleaned, fallback = await clean_bundle(fetched, app)
    assert not fallback and ARTICLE in cleaned.text
    assert REGISTRY in cleaned.urls
    assert all("PREPARE" not in link for link in cleaned.urls)
    assert env.text == url  # audit input stays intact
