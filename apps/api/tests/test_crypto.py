import pytest
from cryptography.exceptions import InvalidTag


def test_secret_roundtrip_and_nonce_changes():
    from mails_api.security.crypto import decrypt_secret, encrypt_secret

    key = bytes(range(32))
    first = encrypt_secret(key, "test-key")
    assert first != encrypt_secret(key, "test-key")
    assert decrypt_secret(key, first) == "test-key"
    assert b"test-key" not in first
    with pytest.raises(InvalidTag):
        decrypt_secret(bytes(reversed(key)), first)
    with pytest.raises(InvalidTag):
        decrypt_secret(key, first[:-1] + bytes([first[-1] ^ 1]))


@pytest.mark.parametrize("key", [b"", bytes(16), bytes(24)])
def test_requires_256_bit_key(key):
    from mails_api.security.crypto import encrypt_secret

    with pytest.raises(ValueError):
        encrypt_secret(key, "test-key")
