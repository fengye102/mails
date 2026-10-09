from mails_api.security.passwords import hash_password, verify_password


def test_password_hash_is_argon2id():
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("$argon2id$")
    assert verify_password(encoded, "correct horse battery staple")
    assert not verify_password(encoded, "wrong")


def test_malformed_password_hash_is_rejected():
    assert not verify_password("not-a-hash", "secret")
