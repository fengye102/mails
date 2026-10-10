
from delivery_api.security import hash_secret, verify_secret


def test_secret_hash_is_not_reversible_and_verifies():
    digest = hash_secret("secret")
    assert digest != "secret"
    assert verify_secret("secret", digest)
    assert not verify_secret("wrong", digest)


def test_pickup_code_hash_is_deterministic():
    assert hash_secret("code") == hash_secret("code")
