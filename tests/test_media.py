import io

from PIL import Image, ImageDraw

from tg_x_copilot.pipeline.media import inspect_image, optimize_image
from tg_x_copilot.services.storage import asset_key

OPTS = dict(photo_max_side=2048, graphic_max_side=4096, jpeg_quality=85)


def _png_screenshot(w=1200, h=800) -> bytes:
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for y in range(20, h, 40):
        d.text((20, y), "Quarterly revenue rose 12% to $3.4B", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _noisy_photo(w=3000, h=2000) -> bytes:
    import random

    rnd = random.Random(1)
    img = Image.new("RGB", (w // 10, h // 10))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                 for _ in range((w // 10) * (h // 10))])
    img = img.resize((w, h))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_screenshot_stays_lossless_png():
    out = optimize_image(_png_screenshot(), graphic=None, **OPTS)
    assert out.mime == "image/png"
    assert (out.width, out.height) == (1200, 800)


def test_photo_becomes_bounded_jpeg():
    src = _noisy_photo()
    out = optimize_image(src, graphic=None, **OPTS)
    assert out.mime == "image/jpeg"
    assert max(out.width, out.height) <= 2048
    assert out.size < len(src)


def test_clean_small_jpeg_kept_byte_for_byte():
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (120, 30, 200)).save(buf, "JPEG", quality=80)
    data = buf.getvalue()
    out = optimize_image(data, graphic=False, **OPTS)
    assert out.data == data


def test_exif_is_dropped():
    img = Image.new("RGB", (400, 300), "red")
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # Make
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    out = optimize_image(buf.getvalue(), graphic=False, **OPTS)
    assert b"SecretCam" not in out.data


def test_asset_key_is_content_addressed():
    blob = inspect_image(_png_screenshot())
    assert blob is not None
    key = asset_key(blob)
    assert key == f"assets/{blob.sha256[:2]}/{blob.sha256}.png"
    assert asset_key(inspect_image(_png_screenshot())) == key  # same bytes -> same key


def test_inspect_rejects_non_images():
    assert inspect_image(b"not an image") is None
