from typing import Annotated

import pytest
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from mails_api.models import Session, User


def login(client, headers):
    return client.post(
        "/api/v1/session",
        headers=headers,
        json={"username": "owner", "password": "secret"},
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://foreign.example"},
        {"Origin": "null"},
        {"Origin": "http://localhost:5173.evil.example"},
        {"Origin": "http://localhost.evil.example:5173"},
        {"Origin": "http://localhost:5174"},
        {"Origin": "https://localhost:5173"},
        {"Origin": "http://localhost:5173@evil.example"},
        {"Origin": "http://[broken"},
        {"Referer": "http://foreign.example/login"},
        {"Origin": "https://foreign.example", "Referer": "http://localhost:5173/login"},
    ],
)
def test_login_rejects_missing_or_foreign_origin(security_client, engine, headers):
    assert login(security_client, headers).status_code == 403
    with DbSession(engine) as db:
        assert db.scalar(select(Session)) is None


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://localhost:5173"},
        {"Origin": "http://LOCALHOST:5173"},
        {"Referer": "http://localhost:5173/login?next=inbox"},
    ],
)
def test_login_accepts_same_origin(security_client, headers):
    assert login(security_client, headers).status_code == 200


@pytest.mark.parametrize("csrf", [None, "wrong", b"\xc3\xa9"])
def test_logout_rejects_invalid_csrf_without_revoking(security_client, csrf):
    assert (
        login(security_client, {"Origin": "http://localhost:5173"}).status_code == 200
    )
    headers = {} if csrf is None else {"X-CSRF-Token": csrf}
    assert security_client.delete("/api/v1/session", headers=headers).status_code == 403
    assert security_client.get("/api/v1/session").status_code == 200


def test_logout_accepts_current_csrf(security_client):
    response = login(security_client, {"Origin": "http://localhost:5173"})
    assert (
        security_client.delete(
            "/api/v1/session", headers={"X-CSRF-Token": response.json()["csrf_token"]}
        ).status_code
        == 204
    )
    assert security_client.get("/api/v1/session").status_code == 401


@pytest.mark.parametrize("role,status", [("owner", 200), ("member", 403)])
def test_owner_dependency_enforces_role(security_client, engine, role, status):
    from mails_api.security.csrf import require_owner

    @security_client.app.get("/owner-test")
    def owner_route(user: Annotated[User, Depends(require_owner)]):
        return {"username": user.username}

    assert security_client.get("/owner-test").status_code == 401
    assert (
        login(security_client, {"Origin": "http://localhost:5173"}).status_code == 200
    )
    with DbSession(engine) as db:
        db.scalar(select(User)).role = role
        db.commit()
    assert security_client.get("/owner-test").status_code == status


def test_csrf_dependency_rejects_token_from_another_session(security_client):
    from mails_api.security.csrf import require_csrf

    @security_client.app.post("/csrf-test", dependencies=[Depends(require_csrf)])
    def write_route():
        return {"written": True}

    assert security_client.post("/csrf-test").status_code == 401
    first = login(security_client, {"Origin": "http://localhost:5173"})
    second = login(security_client, {"Origin": "http://localhost:5173"})
    assert (
        security_client.post(
            "/csrf-test", headers={"X-CSRF-Token": first.json()["csrf_token"]}
        ).status_code
        == 403
    )
    assert (
        security_client.post(
            "/csrf-test", headers={"X-CSRF-Token": second.json()["csrf_token"]}
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "configured,provided,status",
    [
        ("https://localhost", "https://localhost:443", 200),
        ("http://localhost:80", "http://localhost", 200),
        ("https://localhost", "https://localhost:80", 403),
    ],
)
def test_login_normalizes_default_ports(security_client, configured, provided, status):
    security_client.app.state.settings.public_origin = configured
    assert login(security_client, {"Origin": provided}).status_code == status


def test_owner_dependency_rejects_missing_user(security_client, engine):
    from mails_api.security.csrf import require_owner

    @security_client.app.get("/owner-test")
    def owner_route(user: Annotated[User, Depends(require_owner)]):
        return {"username": user.username}

    assert (
        login(security_client, {"Origin": "http://localhost:5173"}).status_code == 200
    )
    with DbSession(engine) as db:
        db.delete(db.scalar(select(User)))
        db.commit()
    assert security_client.get("/owner-test").status_code == 401
