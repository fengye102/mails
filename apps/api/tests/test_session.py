import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from mails_api.app import create_app
from mails_api.models import Base, Session, User
from mails_api.security.passwords import hash_password, verify_password
from mails_api.security.sessions import COOKIE_NAME, create_session
from mails_api.settings import Settings
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession


@pytest.fixture
def owner(engine):
    Base.metadata.create_all(engine)
    with DbSession(engine) as db:
        user = User(username="owner", password_hash=hash_password("secret"))
        db.add(user)
        db.commit()
        return user.id


@pytest.fixture
def client(engine, owner):
    settings = Settings(
        database_url="sqlite://",
        encryption_key="test",
        public_origin="http://testserver",
        upstream_origins=[],
    )
    with TestClient(create_app(settings=settings, engine=engine)) as client:
        yield client


def login(client):
    return client.post(
        "/api/v1/session",
        headers={"Origin": str(client.base_url).rstrip("/")},
        json={"username": "owner", "password": "secret"},
    )


def test_logout_revokes_server_session(client, engine):
    assert login(client).status_code == 200
    raw_cookie = client.cookies.get(COOKIE_NAME)
    response = client.get("/api/v1/session")
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "owner"
    assert response.json()["user"]["role"] == "owner"
    assert response.headers["cache-control"] == "no-store"
    csrf = response.json()["csrf_token"]
    assert (
        client.delete("/api/v1/session", headers={"X-CSRF-Token": csrf}).status_code
        == 204
    )
    assert client.get("/api/v1/session").status_code == 401
    client.cookies.set(COOKIE_NAME, raw_cookie)
    assert client.get("/api/v1/session").status_code == 401
    with DbSession(engine) as db:
        assert db.scalar(select(Session)).revoked_at is not None


def test_session_token_is_hashed_and_csrf_independent(engine, owner):
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    with DbSession(engine) as db:
        raw, session = create_session(db, db.get(User, owner), now)
        assert session.token_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert session.csrf_token != raw
        assert len(raw) >= 43
        assert session.created_at == session.last_seen_at == now
        assert session.expires_at == now + timedelta(hours=8)
        db.commit()
        assert db.scalar(select(Session)).token_hash != raw


@pytest.mark.parametrize(
    "username,password", [("owner", "wrong"), ("unknown", "secret")]
)
def test_wrong_credentials_do_not_create_session(client, engine, username, password):
    response = client.post(
        "/api/v1/session",
        headers={"Origin": "http://testserver"},
        json={"username": username, "password": password},
    )
    assert response.status_code == 401
    assert not client.cookies
    with DbSession(engine) as db:
        assert db.scalar(select(Session)) is None


def test_missing_and_unknown_cookie_are_rejected(client):
    assert client.get("/api/v1/session").status_code == 401
    client.cookies.set(COOKIE_NAME, "unknown")
    assert client.get("/api/v1/session").status_code == 401


@pytest.mark.parametrize(
    "field,age", [("last_seen_at", timedelta(minutes=31)), ("expires_at", timedelta(0))]
)
def test_expired_session_is_rejected(client, engine, monkeypatch, field, age):
    assert login(client).status_code == 200
    now = datetime.now(timezone.utc)
    monkeypatch.setattr("mails_api.security.sessions.utc_now", lambda: now)
    with DbSession(engine) as db:
        setattr(db.scalar(select(Session)), field, now - age)
        db.commit()
    assert client.get("/api/v1/session").status_code == 401


def test_active_session_updates_last_seen(client, engine, monkeypatch):
    assert login(client).status_code == 200
    now = datetime.now(timezone.utc) + timedelta(minutes=10)
    monkeypatch.setattr("mails_api.security.sessions.utc_now", lambda: now)
    assert client.get("/api/v1/session").status_code == 200
    with DbSession(engine) as db:
        assert (
            db.scalar(select(Session)).last_seen_at.replace(tzinfo=timezone.utc) == now
        )


@pytest.mark.parametrize("csrf", [None, "wrong"])
def test_logout_rejects_missing_or_wrong_csrf(client, csrf):
    assert login(client).status_code == 200
    headers = {} if csrf is None else {"X-CSRF-Token": csrf}
    assert client.delete("/api/v1/session", headers=headers).status_code == 403
    assert client.get("/api/v1/session").status_code == 200


@pytest.mark.parametrize(
    "origin,secure", [("http://testserver", False), ("https://testserver", True)]
)
def test_cookie_flags(engine, owner, origin, secure):
    settings = Settings(
        database_url="sqlite://",
        encryption_key="test",
        public_origin=origin,
        upstream_origins=[],
    )
    with TestClient(
        create_app(settings=settings, engine=engine), base_url=origin
    ) as client:
        response = login(client)
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Path=/" in cookie
        assert "Domain=" not in cookie
        assert ("Secure" in cookie) is secure
        assert response.headers["cache-control"] == "no-store"


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    url = f"sqlite:///{tmp_path / 'owner.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    for key, value in {
        "MAILS_DATABASE_URL": url,
        "MAILS_ENCRYPTION_KEY": "test",
        "MAILS_PUBLIC_ORIGIN": "http://localhost",
        "MAILS_UPSTREAM_ORIGINS": "[]",
    }.items():
        monkeypatch.setenv(key, value)
    yield engine
    engine.dispose()


def test_cli_creates_owner_and_refuses_second(cli_env, monkeypatch):
    from mails_api.cli import main

    answers = iter(["secret", "secret"])
    monkeypatch.setattr("mails_api.cli.getpass.getpass", lambda prompt: next(answers))
    assert main(["create-owner", "owner"]) == 0
    with DbSession(cli_env) as db:
        user = db.scalar(select(User))
        assert user.username == "owner" and user.role == "owner"
        assert verify_password(user.password_hash, "secret")
    assert main(["create-owner", "another"]) == 1
    with DbSession(cli_env) as db:
        assert len(db.scalars(select(User)).all()) == 1


def test_cli_password_confirmation_mismatch_creates_no_owner(cli_env, monkeypatch):
    from mails_api.cli import main

    answers = iter(["secret", "other"])
    monkeypatch.setattr("mails_api.cli.getpass.getpass", lambda prompt: next(answers))
    assert main(["create-owner", "owner"]) == 1
    with DbSession(cli_env) as db:
        assert db.scalar(select(User)) is None


def test_cli_never_accepts_password_argument(cli_env):
    from mails_api.cli import main

    with pytest.raises(SystemExit) as error:
        main(["create-owner", "owner", "secret"])
    assert error.value.code == 2
