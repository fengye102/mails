from __future__ import annotations

import base64

import httpx
from fastapi.testclient import TestClient
from mails_api.app import create_app
from mails_api.models import Base, User
from mails_api.security.passwords import hash_password
from mails_api.settings import Settings
from sqlalchemy.orm import Session

KEY = bytes(range(32))


def test_cloudflare_and_cymail_connections_register_mailboxes(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(username="owner", password_hash=hash_password("secret")))
        db.commit()

    raw = (
        "From: sender@example.com\r\n"
        "To: alias@icloud.com\r\n"
        "Subject: Verify\r\n"
        "Date: Wed, 09 Jul 2026 06:32:10 GMT\r\n"
        "Message-ID: <message@example.com>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\nVerification code: 123456\r\n"
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cf.example":
            assert request.headers["X-Admin-Auth"] == "cf-secret"
            if request.url.path == "/health_check":
                return httpx.Response(200, text="OK")
            if request.url.path == "/admin/mails":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "cf-1",
                                "address": request.url.params["address"],
                                "created_at": "2026-07-09T06:32:10Z",
                                "raw": raw,
                            }
                        ],
                        "count": 1,
                    },
                )
        if request.url.host == "cy.example":
            assert request.headers["X-API-Key"] == "cy-secret"
            if request.url.path == "/api/accounts":
                return httpx.Response(200, json={"success": True, "data": []})
            if request.url.path == "/api/aliases":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "account_id": "acc-1",
                            "aliases": [{"email": "alias@icloud.com", "active": True}],
                        },
                    },
                )
        raise AssertionError(f"unexpected upstream request: {request.method} {request.url}")

    settings = Settings(
        database_url="sqlite://",
        encryption_key=base64.b64encode(KEY).decode(),
        public_origin="http://localhost:5173",
        upstream_origins=["https://cf.example", "https://cy.example"],
    )
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    with TestClient(create_app(settings, engine, async_client)) as client:
        login = client.post(
            "/api/v1/session",
            headers={"Origin": settings.public_origin},
            json={"username": "owner", "password": "secret"},
        )
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}

        cloudflare = client.post(
            "/api/v1/connections",
            headers=csrf,
            json={
                "kind": "cloudflare_temp_email",
                "name": "Cloudflare",
                "base_url": "https://cf.example",
                "api_key": "cf-secret",
            },
        )
        assert cloudflare.status_code == 201
        cloudflare_mailbox = client.post(
            f"/api/v1/connections/{cloudflare.json()['id']}/mailboxes",
            headers=csrf,
            json={"address": "alias@icloud.com"},
        )
        assert cloudflare_mailbox.status_code == 201
        assert cloudflare_mailbox.json()["provider_mailbox_key"] == "alias@icloud.com"

        cymail = client.post(
            "/api/v1/connections",
            headers=csrf,
            json={
                "kind": "cymail",
                "name": "CYMail",
                "base_url": "https://cy.example",
                "api_key": "cy-secret",
                "config": {"account_id": "acc-1"},
            },
        )
        assert cymail.status_code == 201
        cymail_mailbox = client.post(
            f"/api/v1/connections/{cymail.json()['id']}/mailboxes",
            headers=csrf,
            json={"address": "alias@icloud.com"},
        )
        assert cymail_mailbox.status_code == 201
        assert cymail_mailbox.json()["provider_mailbox_key"].startswith("cymail:")

        connections = client.get("/api/v1/connections").json()
        assert {item["kind"] for item in connections} == {
            "cloudflare_temp_email",
            "cymail",
        }
        assert next(item for item in connections if item["kind"] == "cymail")["config"] == {
            "account_id": "acc-1"
        }
        assert "cf-secret" not in client.get("/api/v1/connections").text
        assert "cy-secret" not in client.get("/api/v1/connections").text


def test_cymail_requires_account_id(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(username="owner", password_hash=hash_password("secret")))
        db.commit()
    settings = Settings(
        database_url="sqlite://",
        encryption_key=base64.b64encode(KEY).decode(),
        public_origin="http://localhost:5173",
        upstream_origins=["https://cy.example"],
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid config must fail before upstream request")

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    with TestClient(create_app(settings, engine, async_client)) as client:
        login = client.post(
            "/api/v1/session",
            headers={"Origin": settings.public_origin},
            json={"username": "owner", "password": "secret"},
        )
        response = client.post(
            "/api/v1/connections",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={
                "kind": "cymail",
                "name": "CYMail",
                "base_url": "https://cy.example",
                "api_key": "cy-secret",
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "REQUEST_INVALID"
