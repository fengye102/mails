"""凭据加密与脱敏：明文只在内存里，对外视图只有 has_* 布尔。"""

import json

import pytest
from icloud_api.accounts import (
    account_app_password,
    account_apple_session,
    account_cookies,
    encrypt_json,
    encrypt_text,
    redacted_account,
)
from icloud_api.models import ICloudAccount
from icloud_api.security.crypto import decrypt_secret, encrypt_secret


def test_secret_roundtrip_and_ciphertext_hides_plaintext(data_key: bytes) -> None:
    blob = encrypt_secret(data_key, "app-password-secret")
    assert b"app-password-secret" not in blob
    assert decrypt_secret(data_key, blob) == "app-password-secret"
    assert len(blob) > 12


def test_json_credentials_roundtrip(data_key: bytes) -> None:
    cookies = {"X-APPLE-WEBAUTH-USER": "t=abc", "session": "xyz"}
    blob = encrypt_json(data_key, cookies)
    assert b"X-APPLE-WEBAUTH-USER" not in blob
    account = ICloudAccount(
        cookies_enc=blob,
        app_password_enc=encrypt_text(data_key, "aaaa-bbbb-cccc-dddd"),
        apple_session_enc=encrypt_json(data_key, {"dsid": "123"}),
    )
    assert account_cookies(account, data_key) == cookies
    assert account_app_password(account, data_key) == "aaaa-bbbb-cccc-dddd"
    assert account_apple_session(account, data_key)["dsid"] == "123"


def test_empty_credentials_decrypt_to_empty(data_key: bytes) -> None:
    account = ICloudAccount()
    assert account_cookies(account, data_key) == {}
    assert account_app_password(account, data_key) == ""
    assert account_apple_session(account, data_key) == {}


def test_redacted_view_never_contains_secrets(data_key: bytes) -> None:
    account = ICloudAccount(
        name="主号",
        real_email="fengye@icloud.com",
        icloud_email="fengye@icloud.com",
        cookies_enc=encrypt_json(data_key, {"session": "secret-cookie"}),
        app_password_enc=encrypt_text(data_key, "secret-app-password"),
    )
    view = redacted_account(account)
    dumped = json.dumps(view, default=str)
    assert "secret-cookie" not in dumped
    assert "secret-app-password" not in dumped
    assert view["has_cookies"] is True
    assert view["has_app_password"] is True
    assert view["has_apple_session"] is False
    assert view["real_email"] == "fengye@icloud.com"


def test_wrong_key_cannot_decrypt(data_key: bytes) -> None:
    blob = encrypt_secret(data_key, "secret")
    other = bytes((byte + 1) % 256 for byte in data_key)
    with pytest.raises(Exception):  # noqa: B017 - AESGCM raises InvalidTag
        decrypt_secret(other, blob)
