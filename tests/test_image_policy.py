from tg_x_copilot.models import ImageAnalysis, ImageDecision
from tg_x_copilot.pipeline.image_policy import decide


def a(**kw) -> ImageAnalysis:
    return ImageAnalysis(description="x", relevance=0.9, **kw)


def test_watermark_always_review():
    d, _ = decide(a(has_third_party_watermark=True, image_type="chart"), owned_source=True,
                  target_language="en")
    assert d == ImageDecision.REVIEW


def test_third_party_never_kept():
    d, _ = decide(a(image_type="illustration", quality="high"), owned_source=False,
                  target_language="en")
    assert d == ImageDecision.REGENERATE


def test_third_party_news_photo_not_regenerated():
    d, _ = decide(a(image_type="photo_real_event"), owned_source=False, target_language="en")
    assert d == ImageDecision.REVIEW


def test_owned_good_image_kept():
    d, _ = decide(a(image_type="photo_real_event", quality="high"), owned_source=True,
                  target_language="en")
    assert d == ImageDecision.KEEP


def test_owned_low_quality_enhanced():
    d, _ = decide(a(image_type="chart", quality="low"), owned_source=True, target_language="en")
    assert d == ImageDecision.ENHANCE


def test_owned_foreign_text_localized():
    d, _ = decide(a(image_type="infographic", contains_text=True, text_language="zh"),
                  owned_source=True, target_language="en")
    assert d == ImageDecision.REGENERATE


def test_sensitive_review():
    d, _ = decide(a(sensitive=True), owned_source=True, target_language="en")
    assert d == ImageDecision.REVIEW
