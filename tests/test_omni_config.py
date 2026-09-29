from apps.omni import OmniConfig
from lib.crypto import ENV_NAME, _get_encryption_password, encrypt_value


def test_captcha_key_is_decrypted(monkeypatch):
    encrypted = encrypt_value("astrum-api-key", password="test-password")
    monkeypatch.setenv(ENV_NAME, "test-password")
    monkeypatch.delattr(_get_encryption_password, "cached_password", raising=False)

    try:
        assert OmniConfig.decrypt_captcha_key(encrypted) == "astrum-api-key"
    finally:
        if hasattr(_get_encryption_password, "cached_password"):
            delattr(_get_encryption_password, "cached_password")
