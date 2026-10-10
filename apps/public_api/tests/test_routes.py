import base64
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient
from public_mail_api.app import create_app
from public_mail_api.models import Base, PublicMailbox, utc_now
from public_mail_api.settings import Settings
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool


def make_app(handler):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url="sqlite+pysqlite://",
        upstream_url="https://worker.example",
        admin_key="admin-secret",
        encryption_key=base64.b64encode(b"k" * 32).decode(),
        cookie_secure=False,
        ttl_seconds=3600,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return create_app(settings=settings, engine=engine, upstream_client=client), engine


def upstream_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path == "/admin/new_address":
        return httpx.Response(
            200, json={"address": "demo@example.com", "jwt": "address-jwt"}
        )
    if request.url.path == "/api/mails":
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "message-1",
                        "raw": "From: sender@example.com\n"
                        "To: demo@example.com\n"
                        "Subject: Verification\n\n"
                        "Code: 482901",
                        "created_at": "2026-10-10T00:00:00Z",
                    }
                ]
            },
        )
    if request.url.path == "/api/mails/message-1":
        return httpx.Response(
            200,
            json={
                "id": "message-1",
                "raw": "From: sender@example.com\n"
                "To: demo@example.com\n"
                "Subject: Verification\n\n"
                "Code: 482901",
                "created_at": "2026-10-10T00:00:00Z",
            },
        )
    return httpx.Response(404, json={"secret": "upstream detail"})


def test_create_sets_capability_cookie_and_reads_plain_text_messages():
    app, _engine = make_app(upstream_handler)
    with TestClient(app) as client:
        created = client.post("/public-api/v1/mailboxes")
        assert created.status_code == 201
        payload = created.json()
        assert payload["address"] == "demo@example.com"
        assert payload["status"] == "active"
        assert "jwt" not in payload
        assert "address_jwt_ciphertext" not in payload
        assert client.cookies.get(app.state.settings.capability_cookie_name)

        mailbox = client.get(f"/public-api/v1/mailboxes/{payload['id']}")
        assert mailbox.status_code == 200
        assert mailbox.json()["remaining_seconds"] > 0

        messages = client.get(f"/public-api/v1/mailboxes/{payload['id']}/messages")
        assert messages.status_code == 200
        assert messages.json() == {
            "total": 1,
            "items": [
                {
                    "id": "message-1",
                    "sender": "sender@example.com",
                    "recipient": "demo@example.com",
                    "subject": "Verification",
                    "snippet": "Code: 482901",
                    "received_at": "2026-10-10T00:00:00+00:00",
                    "verification_code": "482901",
                }
            ],
        }

        detail = client.get(
            f"/public-api/v1/mailboxes/{payload['id']}/messages/message-1"
        )
        assert detail.status_code == 200
        assert detail.json()["body_text"] == "Code: 482901"
        assert "upstream detail" not in detail.text


def test_capability_is_required_and_cannot_access_another_session_mailbox():
    app, _engine = make_app(upstream_handler)
    cookie_name = app.state.settings.capability_cookie_name
    with TestClient(app) as first:
        first_mailbox = first.post("/public-api/v1/mailboxes").json()
        first_capability = first.cookies.get(cookie_name)

    with TestClient(app) as anonymous:
        response = anonymous.get(f"/public-api/v1/mailboxes/{first_mailbox['id']}")
        assert response.status_code == 401
        assert response.json()["detail"] == "PUBLIC_MAIL_CAPABILITY_REQUIRED"

    with TestClient(app) as second:
        other_mailbox = second.post("/public-api/v1/mailboxes").json()
        assert other_mailbox["id"] != first_mailbox["id"]

        # Replace, rather than duplicate, the second TestClient's cookie. The
        # first capability must not authorize a mailbox owned by the second.
        second.cookies.clear()
        second.cookies.set(cookie_name, first_capability)
        response = second.get(
            f"/public-api/v1/mailboxes/{other_mailbox['id']}"
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "PUBLIC_MAIL_CAPABILITY_REQUIRED"


def test_one_session_can_own_multiple_mailboxes_under_one_capability():
    app, engine = make_app(upstream_handler)
    cookie_name = app.state.settings.capability_cookie_name
    with TestClient(app) as client:
        mailbox_ids = []
        initial_capability = None
        for _ in range(3):
            response = client.post("/public-api/v1/mailboxes")
            assert response.status_code == 201
            mailbox_ids.append(response.json()["id"])
            current_capability = client.cookies.get(cookie_name)
            initial_capability = initial_capability or current_capability
            assert current_capability == initial_capability

        with __import__("sqlalchemy").orm.Session(engine) as db:
            rows = [db.get(PublicMailbox, mailbox_id) for mailbox_id in mailbox_ids]
            assert len({row.token_hash for row in rows}) == 1


def test_expired_mailbox_returns_fixed_error_and_rate_limit_is_enforced():
    app, engine = make_app(upstream_handler)
    with TestClient(app) as client:
        created = client.post("/public-api/v1/mailboxes").json()
        with __import__("sqlalchemy").orm.Session(engine) as db:
            mailbox = db.get(PublicMailbox, created["id"])
            mailbox.expires_at = utc_now() - timedelta(seconds=1)
            db.commit()
        expired = client.get(f"/public-api/v1/mailboxes/{created['id']}")
        assert expired.status_code == 410
        assert expired.json() == {"detail": "PUBLIC_MAILBOX_EXPIRED"}

    app, _engine = make_app(upstream_handler)
    with TestClient(app) as client:
        for _ in range(3):
            assert client.post("/public-api/v1/mailboxes").status_code == 201
        limited = client.post("/public-api/v1/mailboxes")
        assert limited.status_code == 429
        assert limited.json() == {"detail": "PUBLIC_MAIL_CREATE_RATE_LIMITED"}
