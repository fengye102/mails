from public_mail_api.models import PublicMailbox, utc_now


def test_public_mailbox_defaults_are_independent():
    first = PublicMailbox(address="a@example.com", address_jwt_ciphertext=b"x", token_hash="h", expires_at=utc_now())
    second = PublicMailbox(address="b@example.com", address_jwt_ciphertext=b"y", token_hash="i", expires_at=utc_now())
    assert first.status == "active"
    assert second.id != first.id
    assert first.created_at is not None
