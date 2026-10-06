from tg_x_copilot.models import ImageAnalysis, ImageDecision
from tg_x_copilot.pipeline.image_policy import decide


def a(**kw) -> ImageAnalysis:
    return ImageAnalysis(description="x", relevance=0.9, **kw)


def test_channel_watermark_recreated_without_editing():
    d, _ = decide(a(has_third_party_watermark=True, image_type="chart"), owned_source=True,
                  target_language="en")
    assert d == ImageDecision.RECREATE


def test_third_party_never_kept():
    d, _ = decide(a(image_type="illustration", quality="high"), owned_source=False,
                  target_language="en")
    assert d == ImageDecision.RECREATE


def test_third_party_news_photo_replaced_by_information_card():
    d, _ = decide(a(image_type="photo_real_event"), owned_source=False, target_language="en")
    assert d == ImageDecision.RECREATE


def test_owned_good_image_kept():
    d, _ = decide(a(image_type="photo_real_event", quality="high"), owned_source=True,
                  target_language="en")
    assert d == ImageDecision.KEEP


def test_owned_low_quality_information_recreated():
    d, _ = decide(a(image_type="chart", quality="low"), owned_source=True, target_language="en")
    assert d == ImageDecision.RECREATE


def test_owned_foreign_text_localized():
    d, _ = decide(a(image_type="infographic", contains_text=True, text_language="zh"),
                  owned_source=True, target_language="en")
    assert d == ImageDecision.LOCALIZE


def test_sensitive_review():
    d, _ = decide(a(sensitive=True), owned_source=True, target_language="en")
    assert d == ImageDecision.REVIEW
