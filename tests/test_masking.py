from tg_x_copilot.logging_setup import mask, register_secrets


def test_masks_common_secret_shapes():
    text = ("Authorization: Bearer abc.def-123 key=sk-ABCDEFGHIJKL "
            "bot 123456789:FAKE_fake_FAKE_fake_FAKE_fake_FAKE_x "
            "mysql://tgx:hunter22@127.0.0.1/db password=topsecret")
    out = mask(text)
    for leaked in ("abc.def-123", "sk-ABCDEFGHIJKL", "FAKE_fake_FAKE_fake_FAKE_fake_FAKE_x",
                   "hunter22", "topsecret"):
        assert leaked not in out


def test_masks_registered_values():
    register_secrets(["my-very-own-r2-secret"])
    assert "my-very-own-r2-secret" not in mask("upstream said my-very-own-r2-secret is bad")


def test_presigned_url_signature_masked():
    url = "https://x.r2.cloudflarestorage.com/b/k?X-Amz-Credential=AK%2F1&X-Amz-Signature=deadbeef"
    out = mask(url)
    assert "deadbeef" not in out and "AK%2F1" not in out
