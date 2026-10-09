"""AES-256-GCM secrets encoded as nonce || authenticated ciphertext."""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def decode_key(value: str) -> bytes:
    key = base64.b64decode(value, validate=True)
    if len(key) != 32:
        raise ValueError("Encryption key must contain 32 bytes")
    return key


def encrypt_secret(key: bytes, value: str) -> bytes:
    if len(key) != 32:
        raise ValueError("Encryption key must contain 32 bytes")
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, value.encode("utf-8"), None)


def decrypt_secret(key: bytes, blob: bytes) -> str:
    if len(key) != 32:
        raise ValueError("Encryption key must contain 32 bytes")
    return AESGCM(key).decrypt(blob[:12], blob[12:], None).decode("utf-8")
