from datetime import datetime, timezone

from public_mail_api.security import hash_capability, make_capability, remaining_seconds


def test_capability_is_random_and_stored_only_as_sha256():
    raw = make_capability()
    assert len(raw) >= 32
    assert hash_capability(raw) != raw
    assert hash_capability(raw) == hash_capability(raw)


def test_remaining_seconds_expires_at_zero_floor():
    now = datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc)
    assert remaining_seconds(datetime(2026, 10, 10, 0, 5, tzinfo=timezone.utc), now) == 300
    assert remaining_seconds(datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc), now) == 0
