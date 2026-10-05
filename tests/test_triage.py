from tg_x_copilot.clients.jev import JevResponse, choice, noul, score
from tg_x_copilot.config import JevSettings
from tg_x_copilot.models import InputEnvelope, MediaKind, SourceMedia
from tg_x_copilot.pipeline.triage import build_questions, build_state, decide_route

CFG = JevSettings()
LEGEND = {"0": "a", "1": "b", "2": "c", "3": "d"}


def resp(value: float, conf: float, *, ctype="news", ctype_conf=0.9, promo=0.1, risky=0.1,
         unverified=0.1, media=None) -> JevResponse:
    answers = {
        "value": {"type": "score", "score": value, "confidence": conf, "legend": LEGEND,
                  "probabilities": {k: 0.25 for k in LEGEND}},
        "content_type": {"type": "choice", "choice": ctype, "confidence": ctype_conf,
                         "probabilities": {ctype: ctype_conf}},
        "promotional": {"type": "noul", "noul": promo},
        "risky": {"type": "noul", "noul": risky},
        "unverified": {"type": "noul", "noul": unverified},
    }
    if media is not None:
        answers["media_useful"] = {"type": "noul", "noul": media}
    return JevResponse.model_validate({"model": "jev-1.13.0", "answers": answers,
                                       "usage": {"input_tokens": 10, "output_tokens": 1}})


def test_score_normalized_over_levels():
    r = resp(1.5, 0.8)
    assert abs(r.answers["value"].normalized_score() - 0.5) < 1e-9


def test_confident_low_value_skips():
    t = decide_route(resp(0.3, 0.9), CFG, has_text=True, has_media=False)
    assert t.route == "skip"


def test_uncertain_low_value_escalates_to_llm():
    t = decide_route(resp(0.3, 0.3), CFG, has_text=True, has_media=False)
    assert t.route == "proceed" and t.escalated


def test_promo_skips_even_if_valuable():
    t = decide_route(resp(3.0, 0.9, promo=0.95), CFG, has_text=True, has_media=False)
    assert t.route == "skip"


def test_confident_chat_type_skips():
    t = decide_route(resp(2.0, 0.9, ctype="chat", ctype_conf=0.8), CFG, has_text=True,
                     has_media=False)
    assert t.route == "skip"


def test_risky_routes_to_review():
    t = decide_route(resp(3.0, 0.9, risky=0.7), CFG, has_text=True, has_media=False)
    assert t.route == "review" and not t.escalated


def test_media_not_useful_is_not_analyzed():
    t = decide_route(resp(3.0, 0.9, media=0.1), CFG, has_text=True, has_media=True)
    assert t.route == "proceed" and not t.use_media


def test_questions_are_typed_and_media_question_optional():
    env = InputEnvelope(chat_id=1, user_id=1, message_ids=[1], text="x" * 50)
    q = build_questions(env, "en-US")
    assert q["value"]["type"] == "score" and 2 <= len(q["value"]["criteria"]) <= 10
    assert q["content_type"]["type"] == "choice"
    assert "media_useful" not in q
    assert all(not k.startswith("_") for k in q)
    assert "US" in q["value"]["instructions"]

    env.media = [SourceMedia(message_id=1, kind=MediaKind.PHOTO)]
    assert "media_useful" in build_questions(env, "en-US")
    state = build_state(env, "direct")
    assert state["attachments"] == "1 photo" and isinstance(state["post_text"], str)


def test_builders_validate_shapes():
    assert noul("q?")["type"] == "noul"
    assert choice("q?", {"a": None, "b": "x"})["criteria"] == {"a": None, "b": "x"}
    assert score("q?", ["lo", "hi"])["criteria"] == ["lo", "hi"]
    for bad in (lambda: choice("q", {"a": None}), lambda: score("q", ["one"])):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")
