import base64

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from mails_api.app import create_app
from mails_api.models import Base, Mailbox, ProviderConnection, User
from mails_api.security.passwords import hash_password
from mails_api.settings import Settings

SECRET = "test-key"
BODY = {
    "kind": "outlook_plus",
    "name": "Primary",
    "base_url": "https://mail.example",
    "api_key": SECRET,
}


@pytest.fixture
def connected(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(username="owner", password_hash=hash_password("secret")))
        db.commit()
    state = {"mode": "ok", "requests": []}

    def upstream(request):
        state["requests"].append(request)
        assert request.headers["X-API-Key"] == state.get("expected_key", SECRET)
        if state["mode"] == "redirect":
            return httpx.Response(302, headers={"location": "https://attacker.test"})
        if state["mode"] == "auth":
            return httpx.Response(403, json={"detail": SECRET})
        data = {"status": "ok", "database": "ok"}
        if request.url.path.endswith("capabilities"):
            data = {"features": ["message_list", "message_detail"]}
        elif request.url.path.endswith("account-status"):
            data = {"exists": True, "can_read": state["mode"] != "denied"}
        elif state["mode"] == "unavailable":
            data["database"] = "down"
        return httpx.Response(200, json={"success": True, "code": "OK", "data": data})

    settings = Settings(
        database_url="sqlite://",
        encryption_key=base64.b64encode(bytes(range(32))).decode(),
        public_origin="http://localhost:5173",
        upstream_origins=["https://mail.example", "http://localhost:9000"],
    )
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    with TestClient(create_app(settings, engine, upstream_client)) as client:
        response = client.post(
            "/api/v1/session",
            headers={"Origin": settings.public_origin},
            json={"username": "owner", "password": "secret"},
        )
        headers = {"X-CSRF-Token": response.json()["csrf_token"]}
        yield client, headers, state, settings


def test_connection_never_echoes_secret_and_probe_before_insert(
    connected, engine, caplog
):
    client, headers, state, _ = connected
    response = client.post("/api/v1/connections", headers=headers, json=BODY)
    assert response.status_code == 201
    assert SECRET not in response.text
    assert "encrypted_secret" not in response.text
    assert response.json()["last_checked_at"].endswith("Z")
    assert len(state["requests"]) == 2
    assert client.get("/api/v1/connections").json() == [response.json()]
    assert SECRET not in caplog.text
    with Session(engine) as db:
        connection = db.scalar(select(ProviderConnection))
        assert SECRET.encode() not in connection.encrypted_secret
        from mails_api.security.crypto import decrypt_secret

        assert decrypt_secret(bytes(range(32)), connection.encrypted_secret) == SECRET


@pytest.mark.parametrize(
    "url",
    [
        "https://mail.example.attacker.test",
        "https://user:pass@mail.example",
        "https://mail.example/path",
        "https://mail.example/#frag",
        "https://mail.example?api_key=test-key",
        "http://mail.example",
        "https://mail.example:444",
        "https://mail.example\\@attacker.test",
    ],
)
def test_rejects_unlisted_or_non_origin_targets(connected, url):
    client, headers, state, _ = connected
    response = client.post(
        "/api/v1/connections", headers=headers, json={**BODY, "base_url": url}
    )
    assert response.status_code == 422
    assert SECRET not in response.text
    assert not state["requests"]


@pytest.mark.parametrize(
    "mode,code",
    [
        ("auth", "UPSTREAM_AUTH"),
        ("denied", "UPSTREAM_CONTRACT"),
        ("redirect", "UPSTREAM_CONTRACT"),
        ("unavailable", "UPSTREAM_UNAVAILABLE"),
    ],
)
def test_upstream_denial_does_not_persist_or_echo(connected, engine, mode, code):
    client, headers, state, _ = connected
    connection = client.post("/api/v1/connections", headers=headers, json=BODY).json()
    state["mode"] = mode
    if mode == "denied":
        response = client.post(
            f"/api/v1/connections/{connection['id']}/mailboxes",
            headers=headers,
            json={"address": "a@example.com"},
        )
        with Session(engine) as db:
            assert db.scalar(select(Mailbox)) is None
    else:
        response = client.post("/api/v1/connections", headers=headers, json=BODY)
        assert len(client.get("/api/v1/connections").json()) == 1
    assert response.status_code == 502
    assert response.json()["detail"] == code
    assert SECRET not in response.text


def test_mailbox_registration_normalizes_and_conflicts(connected, engine):
    client, headers, state, _ = connected
    connection = client.post("/api/v1/connections", headers=headers, json=BODY).json()
    path = f"/api/v1/connections/{connection['id']}/mailboxes"
    response = client.post(
        path, headers=headers, json={"address": "A@Example.COM", "display_name": "Main"}
    )
    assert response.status_code == 201
    assert response.json()["provider_mailbox_key"] == "a@example.com"
    assert state["requests"][-1].url.params["email"] == "a@example.com"
    assert client.get("/api/v1/mailboxes").json() == [response.json()]
    assert (
        client.post(
            path, headers=headers, json={"address": "a@example.com"}
        ).status_code
        == 409
    )


def test_requires_session_owner_and_csrf(connected, engine):
    client, headers, _, _ = connected
    connection = client.post("/api/v1/connections", headers=headers, json=BODY).json()
    path = f"/api/v1/connections/{connection['id']}/mailboxes"
    for target, body in [
        ("/api/v1/connections", BODY),
        (path, {"address": "a@example.com"}),
    ]:
        assert client.post(target, json=body).status_code == 403
        assert (
            client.post(target, headers={"X-CSRF-Token": "bad"}, json=body).status_code
            == 403
        )
    with Session(engine) as db:
        user = db.scalar(select(User))
        user.role = "viewer"
        db.commit()
    assert (
        client.post("/api/v1/connections", headers=headers, json=BODY).status_code
        == 403
    )
    client.cookies.clear()
    for target in ["/api/v1/connections", "/api/v1/mailboxes"]:
        assert client.get(target).status_code == 401
    assert (
        client.post(
            path, headers=headers, json={"address": "a@example.com"}
        ).status_code
        == 401
    )


def test_validation_errors_do_not_echo_secret(connected):
    client, headers, _, settings = connected
    for body in [
        {**BODY, "kind": SECRET},
        {**BODY, "api_key": {"value": SECRET}},
        {**BODY, "name": {"value": SECRET}},
    ]:
        response = client.post("/api/v1/connections", headers=headers, json=body)
        assert response.status_code == 422
        assert SECRET not in response.text
    settings.encryption_key = "invalid"
    response = client.post("/api/v1/connections", headers=headers, json=BODY)
    assert response.status_code == 503
    assert SECRET not in response.text


def test_http_loopback_only_in_development(connected):
    client, headers, _, settings = connected
    response = client.post(
        "/api/v1/connections",
        headers=headers,
        json={**BODY, "base_url": "http://localhost:9000"},
    )
    assert response.status_code == 201
    settings.public_origin = "https://mails.example"
    response = client.post(
        "/api/v1/connections",
        headers=headers,
        json={**BODY, "base_url": "http://localhost:9000"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "public_origin",
    [
        "http://mails.example",
        "http://localhost.attacker.test",
        "http://user:pass@localhost:5173",
        "http://localhost:5173/path",
        "http://localhost:5173/",
        "http://localhost:5173#fragment",
        "http://localhost:5173?query",
        "http://localhost:",
    ],
)
def test_http_upstream_requires_valid_loopback_public_origin(connected, public_origin):
    client, headers, state, settings = connected
    settings.public_origin = public_origin
    response = client.post(
        "/api/v1/connections",
        headers=headers,
        json={**BODY, "base_url": "http://localhost:9000"},
    )
    assert response.status_code == 422
    assert not state["requests"]
    assert client.get("/api/v1/connections").json() == []
    assert (
        client.post("/api/v1/connections", headers=headers, json=BODY).status_code
        == 201
    )


@pytest.mark.parametrize(
    "public_origin",
    ["http://localhost:5173", "http://127.0.0.1:5173", "http://[::1]:5173"],
)
def test_http_upstream_allows_genuine_loopback_development(connected, public_origin):
    client, headers, _, settings = connected
    settings.public_origin = public_origin
    response = client.post(
        "/api/v1/connections",
        headers=headers,
        json={**BODY, "base_url": "http://localhost:9000"},
    )
    assert response.status_code == 201


def test_rejects_empty_port(connected):
    client, headers, _, _ = connected
    response = client.post(
        "/api/v1/connections",
        headers=headers,
        json={**BODY, "base_url": "https://mail.example:"},
    )
    assert response.status_code == 422


def test_canonicalizes_origin_before_saving(connected):
    client, headers, _, _ = connected
    response = client.post(
        "/api/v1/connections",
        headers=headers,
        json={**BODY, "base_url": "https://MAIL.example:443/"},
    )
    assert response.status_code == 201
    assert response.json()["base_url"] == "https://mail.example"


def test_mailbox_validation_and_missing_connection(connected):
    client, headers, _, _ = connected
    path = "/api/v1/connections/00000000-0000-0000-0000-000000000000/mailboxes"
    assert (
        client.post(
            path, headers=headers, json={"address": "a@example.com"}
        ).status_code
        == 404
    )
    response = client.post(path, headers=headers, json={"address": SECRET})
    assert response.status_code == 422
    assert SECRET not in response.text


def test_corrupt_secret_does_not_register_mailbox(connected, engine):
    client, headers, _, _ = connected
    connection = client.post("/api/v1/connections", headers=headers, json=BODY).json()
    with Session(engine) as db:
        row = db.scalar(select(ProviderConnection))
        row.encrypted_secret = b"corrupted-ciphertext"
        db.commit()
    response = client.post(
        f"/api/v1/connections/{connection['id']}/mailboxes",
        headers=headers,
        json={"address": "a@example.com"},
    )
    assert response.status_code == 503
    assert client.get("/api/v1/mailboxes").json() == []


@pytest.mark.parametrize(
    "secret",
    [
        "private-密钥",
        "private-\x00",
        "private-\r\n",
        "private-\x7f",
        "private-\t",
        " private-key",
        "private-key ",
    ],
)
def test_api_key_header_invalid_is_safe_422(connected, secret, caplog):
    client, headers, state, _ = connected
    state["expected_key"] = secret
    response = client.post(
        "/api/v1/connections", headers=headers, json={**BODY, "api_key": secret}
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "API_KEY_INVALID"}
    assert "private-" not in response.text + caplog.text
    assert not state["requests"]
    assert client.get("/api/v1/connections").json() == []
