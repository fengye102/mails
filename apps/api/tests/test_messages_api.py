from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from mails_api.models import Mailbox, Message, ProviderConnection, Session, utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession


@pytest.fixture
def messages_client(security_client, engine):
    response = security_client.post(
        "/api/v1/session",
        json={"username": "owner", "password": "secret"},
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 200
    with DbSession(engine) as db:
        connection = ProviderConnection(
            kind="outlook_plus",
            name="source",
            base_url="http://upstream",
            encrypted_secret=b"secret",
            status="active",
        )
        db.add(connection)
        db.flush()
        mailboxes = [
            Mailbox(
                connection_id=connection.id,
                provider_mailbox_key=str(i),
                address=f"box{i}@example.com",
                sync_state="idle",
            )
            for i in range(2)
        ]
        db.add_all(mailboxes)
        db.flush()
        for i, (subject, sender, hour) in enumerate(
            [
                ("Verify account", "Security@Example.com", 12),
                ("100% complete", "percent%@example.com", 13),
                ("under_score", "under_score@example.com", 14),
                (r"path\name", r"path\name@example.com", 14),
                ("ordinary", "ordinary@example.com", 15),
            ],
            1,
        ):
            db.add(
                Message(
                    id=UUID(int=i),
                    mailbox_id=mailboxes[(i - 1) % 2].id,
                    provider_message_key=f"graph:{i}",
                    sender=sender,
                    recipient="owner@example.com",
                    subject=subject,
                    snippet="summary only",
                    received_at=datetime(2026, 10, 9, hour, tzinfo=timezone.utc),
                    body_text='Verification code: 123456\n<script>alert("x")</script>',
                    verification_code="123456" if i == 1 else None,
                )
            )
        db.commit()
    return security_client


def test_private_summaries_and_plain_text_detail(messages_client):
    response = messages_client.get("/api/v1/messages", params={"subject": "vErIfY"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert set(item) == {
        "id",
        "mailbox_id",
        "source_kind",
        "identity_status",
        "sender",
        "recipient",
        "subject",
        "snippet",
        "received_at",
        "verification_code",
    }
    assert item["received_at"] == "2026-10-09T12:00:00+00:00"
    assert item["source_kind"] == "outlook_plus"
    assert item["identity_status"] == "unverified"
    response = messages_client.get(f"/api/v1/messages/{item['id']}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    detail = response.json()
    assert set(detail) == set(item) | {"body_text"}
    assert (
        detail["body_text"] == 'Verification code: 123456\n<script>alert("x")</script>'
    )
    assert detail["verification_code"] == "123456"
    assert detail["source_kind"] == "outlook_plus"
    assert detail["identity_status"] == "unverified"
    assert (
        messages_client.get(f"/api/v1/messages/{UUID(int=2)}").json()[
            "verification_code"
        ]
        is None
    )


def test_pagination_newest_first_and_stable_ties(messages_client):
    response = messages_client.get("/api/v1/messages", params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 5
    assert [i["id"] for i in response.json()["items"]] == [
        str(UUID(int=4)),
        str(UUID(int=3)),
    ]
    assert len(messages_client.get("/api/v1/messages").json()["items"]) == 5
    assert messages_client.get(
        "/api/v1/messages", params={"offset": 10000, "limit": 100}
    ).json() == {"total": 5, "items": []}


def test_combined_mailbox_sender_subject_and_time_filter(messages_client):
    mailbox_id = messages_client.get(f"/api/v1/messages/{UUID(int=1)}").json()[
        "mailbox_id"
    ]
    response = messages_client.get(
        "/api/v1/messages",
        params={
            "mailbox_id": mailbox_id,
            "sender": "SECURITY",
            "subject": "ACCOUNT",
            "from": "2026-10-09T12:00:00Z",
            "to": "2026-10-09T13:00:00Z",
        },
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(UUID(int=1))]
    response = messages_client.get(
        "/api/v1/messages",
        params={
            "from": "2026-10-09T20:00:00+08:00",
            "to": "2026-10-09T21:00:00+08:00",
        },
    )
    assert response.json()["total"] == 1
    assert messages_client.get(
        "/api/v1/messages", params={"mailbox_id": str(uuid4())}
    ).json() == {"total": 0, "items": []}


def test_query_source_kind_and_connection_filters(messages_client, engine):
    with DbSession(engine) as db:
        connection = db.scalar(select(ProviderConnection))
        response_mail = db.get(Message, UUID(int=1))
        response_mail.rfc_message_id = "<same@example.com>"
        extra_mail = Message(
            mailbox_id=response_mail.mailbox_id,
            provider_message_key="imap:1",
            rfc_message_id="<same@example.com>",
            sender="other@example.com",
            recipient="owner@example.com",
            subject="different",
            snippet="other body",
            received_at=datetime(2026, 10, 9, 16, tzinfo=timezone.utc),
            body_text="needle in body",
        )
        db.add(extra_mail)
        db.commit()
        extra_mail_id = extra_mail.id
        connection_id = str(connection.id)

    response = messages_client.get(
        "/api/v1/messages",
        params={
            "query": "needle",
            "source_kind": "outlook_plus",
            "connection_id": connection_id,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == str(extra_mail_id)
    assert payload["items"][0]["source_kind"] == "outlook_plus"


def test_query_treats_sql_wildcards_as_literals(messages_client):
    response = messages_client.get("/api/v1/messages", params={"query": "%"})
    assert response.status_code == 200
    assert response.json()["total"] == 1


@pytest.mark.parametrize("field", ["sender", "subject"])
@pytest.mark.parametrize(
    "value, message_id", [("%", 2), ("_", 3), ("\\", 4), ("' OR 1=1 --", None)]
)
def test_filters_treat_wildcards_and_sql_as_literals(
    messages_client, field, value, message_id
):
    response = messages_client.get("/api/v1/messages", params={field: value})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == (
        [] if message_id is None else [str(UUID(int=message_id))]
    )


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"offset": 10001},
        {"mailbox_id": "bad"},
        {"from": "bad"},
        {"to": "bad"},
        {"from": "2026-10-09T12:00:00"},
        {"to": "2026-10-09"},
    ],
)
def test_invalid_search_parameters(messages_client, params):
    assert messages_client.get("/api/v1/messages", params=params).status_code == 422


def test_missing_message(messages_client):
    assert messages_client.get(f"/api/v1/messages/{uuid4()}").status_code == 404
    assert messages_client.get("/api/v1/messages/not-a-uuid").status_code == 422


@pytest.mark.parametrize(
    "path", ["/api/v1/messages", f"/api/v1/messages/{UUID(int=1)}"]
)
def test_anonymous_read_is_private(security_client, path):
    assert security_client.get(path).status_code == 401


@pytest.mark.parametrize("state", ["revoked", "expired", "idle", "unknown"])
def test_inaccessible_session_on_both_routes(messages_client, engine, state):
    with DbSession(engine) as db:
        session = db.scalar(select(Session))
        if state == "revoked":
            session.revoked_at = utc_now()
        elif state == "expired":
            session.expires_at = utc_now() - timedelta(seconds=1)
        elif state == "idle":
            session.last_seen_at = utc_now() - timedelta(minutes=31)
        else:
            session.token_hash = "unknown"
        db.commit()
    for path in ["/api/v1/messages", f"/api/v1/messages/{UUID(int=1)}"]:
        response = messages_client.get(path)
        assert response.status_code == 401
        assert "123456" not in response.text


def test_mailbox_filter_is_exact(messages_client):
    mailbox_id = messages_client.get(f"/api/v1/messages/{UUID(int=1)}").json()[
        "mailbox_id"
    ]
    response = messages_client.get(
        "/api/v1/messages", params={"mailbox_id": mailbox_id}
    )
    assert response.json()["total"] == 3
    assert [item["id"] for item in response.json()["items"]] == [
        str(UUID(int=i)) for i in [5, 3, 1]
    ]


def test_default_limit_is_fifty(messages_client, engine):
    with DbSession(engine) as db:
        mailbox_id = db.get(Message, UUID(int=1)).mailbox_id
        db.add_all(
            [
                Message(
                    mailbox_id=mailbox_id,
                    provider_message_key=f"extra:{i}",
                    sender="sender@example.com",
                    recipient="owner@example.com",
                    subject="extra",
                    snippet="summary",
                    body_text="private",
                    received_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
                )
                for i in range(60)
            ]
        )
        db.commit()
    response = messages_client.get("/api/v1/messages")
    assert response.json()["total"] == 65
    assert len(response.json()["items"]) == 50
    assert (
        len(
            messages_client.get("/api/v1/messages", params={"limit": 1}).json()["items"]
        )
        == 1
    )
