import base64

from public_mail_api.security import decrypt_address_jwt, encrypt_address_jwt


def test_address_jwt_round_trip_and_random_nonce():
    key = b"k" * 32
    first = encrypt_address_jwt(key, "jwt-value")
    second = encrypt_address_jwt(key, "jwt-value")
    assert first != second
    assert decrypt_address_jwt(key, first) == "jwt-value"
    assert base64.b64encode(first).decode()
