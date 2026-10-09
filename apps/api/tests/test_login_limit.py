from datetime import datetime, timedelta, timezone


def attempt(client, username="owner", password="wrong", headers=None):
    return client.post(
        "/api/v1/session",
        headers={"Origin": "http://localhost:5173", **(headers or {})},
        json={"username": username, "password": password},
    )


def test_five_failures_block_even_correct_password(security_client):
    for _ in range(5):
        assert attempt(security_client).status_code == 401
    assert attempt(security_client, password="secret").status_code == 429
    assert attempt(security_client, username="other").status_code == 401
    assert attempt(security_client, username="OWNER").status_code == 429
    assert (
        attempt(security_client, headers={"X-Forwarded-For": "203.0.113.2"}).status_code
        == 429
    )


def test_success_resets_failure_bucket(security_client):
    for _ in range(4):
        assert attempt(security_client).status_code == 401
    assert attempt(security_client, password="secret").status_code == 200
    for _ in range(5):
        assert attempt(security_client).status_code == 401
    assert attempt(security_client).status_code == 429


def test_limiter_window_and_peer_isolation():
    from mails_api.security.login_limit import LoginLimiter

    limiter = LoginLimiter()
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    for _ in range(5):
        limiter.record_failure("Owner", "peer-one", now)
    assert limiter.is_limited("OWNER", "peer-one", now)
    assert not limiter.is_limited("owner", "peer-two", now)
    assert not limiter.is_limited("other", "peer-one", now)
    assert limiter.is_limited(
        "owner", "peer-one", now + timedelta(minutes=15) - timedelta(microseconds=1)
    )
    assert not limiter.is_limited("owner", "peer-one", now + timedelta(minutes=15))


def test_http_bucket_expires(security_client, monkeypatch):
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    monkeypatch.setattr("mails_api.routes.session.utc_now", lambda: now)
    for _ in range(5):
        assert attempt(security_client).status_code == 401
    assert attempt(security_client).status_code == 429
    now += timedelta(minutes=15)
    assert attempt(security_client, password="secret").status_code == 200
