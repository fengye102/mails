"""Hash-only capabilities and AES-256-GCM address JWT storage."""
import base64
import hashlib
import os
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def make_capability() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def hash_capability(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def decode_encryption_key(value: str | bytes) -> bytes:
    if isinstance(value, str):
        try:
            value = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            raise ValueError("PUBLIC_MAIL_ENCRYPTION_KEY must be base64") from None
    if len(value) != 32:
        raise ValueError("PUBLIC_MAIL_ENCRYPTION_KEY must contain 32 bytes")
    return bytes(value)


def encrypt_address_jwt(key: bytes, value: str) -> bytes:
    if len(key) != 32:
        raise ValueError("encryption key must contain 32 bytes")
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, value.encode("utf-8"), None)


def decrypt_address_jwt(key: bytes, blob: bytes) -> str:
    if len(key) != 32 or len(blob) < 28:
        raise ValueError("invalid encrypted address JWT")
    return AESGCM(key).decrypt(blob[:12], blob[12:], None).decode("utf-8")


def remaining_seconds(expires_at: datetime, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0, int((expires_at - now).total_seconds()))
