
import pytest
from delivery_api.app import create_app
from delivery_api.models import Base
from delivery_api.settings import Settings
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

OPERATOR = "operator-test-token"


@pytest.fixture
def client():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    app = create_app(
        Settings(database_url="sqlite+pysqlite:///:memory:", operator_token=OPERATOR, encryption_key="test-encryption-key", pickup_ttl_seconds=300),
        engine=engine,
    )
    with TestClient(app) as test_client:
        yield test_client


def test_operator_can_register_and_allocate_mailbox(client):
    created = client.post(
        "/delivery-api/v1/inventory/mailboxes",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={
            "address": "a@example.test",
            "source_label": "cymail",
            "access_token": "opaque-token",
            "messages": [{"id": "m1", "subject": "Code", "sender": "noreply@example.test", "received_at": "2026-10-10T00:00:00Z", "body_text": "Your code is 123456"}],
        },
    )
    assert created.status_code == 201
    mailbox_id = created.json()["id"]
    allocated = client.post(
        "/delivery-api/v1/inventory/allocations",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={"mailbox_id": mailbox_id, "external_ref": "order-1", "ttl_seconds": 600},
    )
    assert allocated.status_code == 201
    assert allocated.json()["status"] == "active"
    duplicate = client.post(
        "/delivery-api/v1/inventory/allocations",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={"mailbox_id": mailbox_id, "external_ref": "order-2", "ttl_seconds": 600},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "DELIVERY_MAILBOX_NOT_AVAILABLE"


def test_pickup_code_is_one_time_and_only_returns_plaintext(client):
    mailbox = client.post(
        "/delivery-api/v1/inventory/mailboxes",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={"address": "b@example.test", "source_label": "cloudflare", "messages": []},
    ).json()
    allocation = client.post(
        "/delivery-api/v1/inventory/allocations",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={"mailbox_id": mailbox["id"], "external_ref": "order-3", "ttl_seconds": 600},
    ).json()
    pickup = client.post(
        "/delivery-api/v1/pickups",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={"allocation_id": allocation["id"], "ttl_seconds": 300},
    )
    assert pickup.status_code == 201
    payload = pickup.json()
    assert payload["pickup_code"]
    assert "code_hash" not in payload
    verified = client.post("/delivery-api/v1/pickups/verify", json={"pickup_code": payload["pickup_code"]})
    assert verified.status_code == 200
    session_token = verified.json()["session_token"]
    assert client.post("/delivery-api/v1/pickups/verify", json={"pickup_code": payload["pickup_code"]}).status_code == 401
    mailbox_response = client.get(
        f"/delivery-api/v1/pickups/{allocation['id']}/mailbox",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert mailbox_response.status_code == 200
    body = mailbox_response.json()
    assert body["address"] == "b@example.test"
    assert body["messages"] == []
    assert "html" not in body


def test_expired_allocation_and_invalid_pickup_are_fixed_errors(client):
    response = client.post("/delivery-api/v1/pickups/verify", json={"pickup_code": "wrong"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "DELIVERY_PICKUP_INVALID"

    mailbox = client.post(
        "/delivery-api/v1/inventory/mailboxes",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={"address": "c@example.test", "source_label": "source", "messages": []},
    ).json()
    allocation = client.post(
        "/delivery-api/v1/inventory/allocations",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={"mailbox_id": mailbox["id"], "external_ref": "expired", "ttl_seconds": 0},
    )
    assert allocation.status_code == 201
    pickup = client.post(
        "/delivery-api/v1/pickups",
        headers={"Authorization": f"Bearer {OPERATOR}"},
        json={"allocation_id": allocation.json()["id"]},
    )
    assert pickup.status_code == 409
    assert pickup.json()["detail"]["code"] == "DELIVERY_ALLOCATION_EXPIRED"



