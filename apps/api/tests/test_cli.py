"""Owner bootstrap rejects unusable credentials before any write."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from mails_api.cli import main
from mails_api.models import Base, User


@pytest.mark.parametrize(
    "username,password",
    [("owner", ""), ("", "secret"), ("   ", "secret"), ("x" * 129, "secret")],
)
def test_cli_rejects_invalid_credentials_without_owner(
    tmp_path, monkeypatch, capsys, username, password
):
    url = "sqlite:///" + str(tmp_path / "owner.db")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MAILS_DATABASE_URL", url)
    monkeypatch.setenv("MAILS_ENCRYPTION_KEY", "test")
    monkeypatch.setenv("MAILS_PUBLIC_ORIGIN", "http://localhost:5173")
    monkeypatch.setenv("MAILS_UPSTREAM_ORIGINS", "[]")
    monkeypatch.setattr("getpass.getpass", lambda _: password)
    try:
        assert main(["create-owner", username]) == 1
        with Session(engine) as db:
            assert db.scalar(select(User)) is None
        output = capsys.readouterr()
        assert "secret" not in output.err and "Owner created" not in output.out
    finally:
        engine.dispose()


@pytest.mark.parametrize("username", ["owner", "x" * 128])
def test_cli_accepts_nonempty_password_and_username_limit(
    tmp_path, monkeypatch, username
):
    from mails_api.security.passwords import verify_password

    url = "sqlite:///" + str(tmp_path / "valid-owner.db")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MAILS_DATABASE_URL", url)
    monkeypatch.setenv("MAILS_ENCRYPTION_KEY", "test")
    monkeypatch.setenv("MAILS_PUBLIC_ORIGIN", "http://localhost:5173")
    monkeypatch.setenv("MAILS_UPSTREAM_ORIGINS", "[]")
    monkeypatch.setattr("getpass.getpass", lambda _: "nonempty-password")
    try:
        assert main(["create-owner", username]) == 0
        with Session(engine) as db:
            user = db.scalar(select(User))
            assert user.username == username
            assert verify_password(user.password_hash, "nonempty-password")
    finally:
        engine.dispose()
