"""引擎侧凭据与对外视图（对应 CYMail `internal/account/manager.go` 的加密与脱敏部分）。

不变量：Cookie、App Password、Apple 会话只在内存里以明文存在；落库一律密文；
任何对外响应只能出现 `has_*` 布尔，不得出现凭据本身。
"""

import json

from icloud_api.security.crypto import decode_key, decrypt_secret, encrypt_secret

SENSITIVE_FIELDS = ("cookies_enc", "app_password_enc", "apple_session_enc")


def resolve_key(encoded_key: str) -> bytes:
    if not encoded_key:
        raise ValueError("ICLOUD_DATA_KEY is required to store credentials")
    return decode_key(encoded_key)


def encrypt_json(key: bytes, value: dict | list | None) -> bytes:
    if not value:
        return b""
    return encrypt_secret(key, json.dumps(value, ensure_ascii=False, sort_keys=True))


def decrypt_json(key: bytes, blob: bytes) -> dict | list:
    if not blob:
        return {}
    return json.loads(decrypt_secret(key, blob))


def encrypt_text(key: bytes, value: str) -> bytes:
    return encrypt_secret(key, value) if value else b""


def decrypt_text(key: bytes, blob: bytes) -> str:
    return decrypt_secret(key, blob) if blob else ""


def account_cookies(account, key: bytes) -> dict[str, str]:
    cookies = decrypt_json(key, account.cookies_enc)
    return cookies if isinstance(cookies, dict) else {}


def account_app_password(account, key: bytes) -> str:
    return decrypt_text(key, account.app_password_enc)


def account_apple_session(account, key: bytes) -> dict:
    state = decrypt_json(key, account.apple_session_enc)
    return state if isinstance(state, dict) else {}


def redacted_account(account) -> dict:
    """账号的对外视图：只报凭据是否存在，不报内容。"""
    return {
        "id": account.id,
        "name": account.name,
        "real_email": account.real_email,
        "icloud_email": account.icloud_email,
        "host": account.host,
        "status": account.status,
        "alias_total": account.alias_total,
        "alias_active": account.alias_active,
        "last_validated": account.last_validated,
        "last_error": account.last_error,
        "created_at": account.created_at,
        "has_cookies": bool(account.cookies_enc),
        "has_app_password": bool(account.app_password_enc),
        "has_apple_session": bool(account.apple_session_enc),
    }
