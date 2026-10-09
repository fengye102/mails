"""A4 member permissions: read-only access and local bootstrap."""

import base64
from pathlib import Path

import pytest
from mails_api.models import Base, User
from mails_api.security.passwords import hash_password
from sqlalchemy import select
from sqlalchemy.orm import Session


@pytest.mark.parametrize("path", [
    "/api/v1/connections",
    "/api/v1/mailboxes",
    "/api/v1/sync-runs",
    "/api/v1/messages",
])
def test_member_can_read_private_views(security_client, engine, path):
    login = security_client.post(
        "/api/v1/session",
        headers={"Origin": "http://localhost:5173"},
        json={"username": "owner", "password": "secret"},
    )
    assert login.status_code == 200
    with Session(engine) as db:
        db.scalar(select(User)).role = "member"
        db.commit()

    assert security_client.get(path).status_code == 200


def test_member_cannot_write_connections_mailboxes_or_sync(security_client, engine):
    login = security_client.post(
        "/api/v1/session",
        headers={"Origin": "http://localhost:5173"},
        json={"username": "owner", "password": "secret"},
    )
    csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
    with Session(engine) as db:
        db.scalar(select(User)).role = "member"
        db.commit()

    assert security_client.post(
        "/api/v1/connections",
        headers=csrf,
        json={
            "kind": "outlook_plus",
            "name": "Primary",
            "base_url": "https://mail.example",
            "api_key": "secret-key",
        },
    ).status_code == 403
    assert security_client.post(
        "/api/v1/connections/00000000-0000-0000-0000-000000000000/mailboxes",
        headers=csrf,
        json={"address": "a@example.com"},
    ).status_code == 403
    assert security_client.post(
        "/api/v1/mailboxes/00000000-0000-0000-0000-000000000000/sync",
        headers=csrf,
    ).status_code == 403


def _configure_db(monkeypatch, path: Path) -> str:
    url = "sqlite:///" + str(path)
    monkeypatch.setenv("MAILS_DATABASE_URL", url)
    monkeypatch.setenv("MAILS_ENCRYPTION_KEY", base64.b64encode(bytes(range(32))).decode())
    monkeypatch.setenv("MAILS_PUBLIC_ORIGIN", "http://localhost:5173")
    monkeypatch.setenv("MAILS_UPSTREAM_ORIGINS", "[]")
    return url


def test_cli_creates_member_only_when_owner_exists(tmp_path, monkeypatch):
    from mails_api.cli import main

    url = _configure_db(monkeypatch, tmp_path / "members.db")
    from sqlalchemy import create_engine

    db_engine = create_engine(url)
    Base.metadata.create_all(db_engine)
    with Session(db_engine) as db:
        db.add(User(username="owner", password_hash=hash_password("owner-pass"), role="owner"))
        db.commit()
    monkeypatch.setattr("mails_api.cli.getpass.getpass", lambda _: "member-pass")

    try:
        assert main(["create-member", "member"]) == 0
        with Session(db_engine) as db:
            member = db.scalar(select(User).where(User.username == "member"))
            assert member is not None
            assert member.role == "member"
    finally:
        db_engine.dispose()


def test_cli_rejects_member_creation_without_owner(tmp_path, monkeypatch, capsys):
    from mails_api.cli import main
    from sqlalchemy import create_engine

    url = _configure_db(monkeypatch, tmp_path / "no-owner.db")
    db_engine = create_engine(url)
    Base.metadata.create_all(db_engine)
    monkeypatch.setattr("mails_api.cli.getpass.getpass", lambda _: "member-pass")
    try:
        assert main(["create-member", "member"]) == 1
        assert "owner" in capsys.readouterr().err.lower()
        with Session(db_engine) as db:
            assert db.scalar(select(User)) is None
    finally:
        db_engine.dispose()
