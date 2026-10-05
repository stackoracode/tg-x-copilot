from tg_x_copilot.models import Claim, RewriteResult
from tg_x_copilot.pipeline.guards import XRules, check_rewrite, numbers_in, x_length

RULES = XRules(max_chars=280, max_hashtags=1, max_emojis=2,
               banned_phrases=("you won't believe", "shocking"))
SOURCE = "Компания сообщила о росте выручки на 12% до $3,400 млн во втором квартале."


def _r(post: str, **kw) -> RewriteResult:
    base = dict(hook=post.split(".")[0], added_value="Explains what the growth means for buyers.",
                claims=[Claim(text="Revenue grew 12%", basis="source")])
    base.update(kw)
    return RewriteResult(post=post, **base)


def test_clean_post_passes():
    post = "Revenue up 12% to $3,400M in Q2. Why it matters: more cash for price cuts this fall."
    report = check_rewrite(_r(post), source_text=SOURCE, rules=RULES,
                           allowed_facts=["Q2 means the second quarter"])
    assert report.ok, report.problems


def test_fabricated_number_is_flagged():
    report = check_rewrite(_r("Revenue up 15% this quarter."), source_text=SOURCE, rules=RULES)
    assert any("15" in p for p in report.problems)


def test_clickbait_and_hashtags_flagged():
    post = "SHOCKING: revenue up 12%! #biz #money"
    report = check_rewrite(_r(post), source_text=SOURCE, rules=RULES)
    joined = " ".join(report.problems)
    assert "clickbait" in joined and "hashtags" in joined


def test_mere_translation_flagged():
    report = check_rewrite(_r("Revenue grew 12%.", is_mere_translation=True),
                           source_text=SOURCE, rules=RULES)
    assert not report.ok


def test_too_similar_to_english_source():
    src = "The city council approved a new bike lane plan for downtown streets on Monday."
    report = check_rewrite(_r(src), source_text=src, rules=RULES)
    assert any("Too close" in p for p in report.problems)


def test_length_counts_urls_as_23():
    assert x_length("see https://example.com/" + "a" * 100) == len("see ") + 23


def test_numbers_normalized():
    assert numbers_in("$3,400 and 12.5%") == {"3400", "12.5"}


def test_background_claims_are_warnings_not_problems():
    post = "Revenue up 12% in Q2. Why it matters: margins."
    r = _r(post, claims=[Claim(text="Q2 ends in June", basis="background")])
    report = check_rewrite(r, source_text=SOURCE, rules=RULES, allowed_facts=["Q2"])
    assert report.ok and report.warnings
