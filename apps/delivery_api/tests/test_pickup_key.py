"""C 取件凭证测试：取件密钥（tok_）、重签、地址匹配、反枚举双桶与安全响应头。"""

from urllib.parse import parse_qs

import pytest
from delivery_api.models import PickupAttempt, PickupCredential
from delivery_api.security import hash_secret
from sqlalchemy import select
from sqlalchemy.orm import Session

OPERATOR = "operator-test-token"
HEADERS = {"Authorization": f"Bearer {OPERATOR}"}


def prepare(client, address="key@example.test", ttl_seconds=600, messages=None):
    payload = {"address": address, "source_label": "outlook_plus"}
    if messages is not None:
        payload["messages"] = messages
    mailbox = client.post(
        "/delivery-api/v1/inventory/mailboxes", headers=HEADERS, json=payload
    ).json()
    allocation = client.post(
        "/delivery-api/v1/inventory/allocations",
        headers=HEADERS,
        json={
            "mailbox_id": mailbox["id"],
            "external_ref": "order-key",
            "ttl_seconds": ttl_seconds,
        },
    ).json()
    return mailbox, allocation


def issue_key(client, allocation_id, reissue=False):
    return client.post(
        "/delivery-api/v1/pickups",
        headers=HEADERS,
        json={"allocation_id": allocation_id, "mode": "key", "reissue": reissue},
    )


def test_key_mode_returns_delivery_bundle_once(client):
    mailbox, allocation = prepare(client)
    response = issue_key(client, allocation["id"])
    assert response.status_code == 201
    payload = response.json()
    key = payload["pickup_key"]
    assert key.startswith("tok_")
    assert payload["pickup_url"].startswith("/pickup#")
    # 链接里的 email/key 是 URL 编码的（@ 会变成 %40）
    fragment = parse_qs(payload["pickup_url"].split("#", 1)[1])
    assert fragment["key"] == [key]
    assert fragment["email"] == [mailbox["address"]]
    assert payload["delivery_text"] == (
        f"{mailbox['address']}---{key}---{payload['pickup_url']}"
    )
    assert "key_hash" not in payload and "code_hash" not in payload


def test_key_is_reusable_while_code_is_one_time(client):
    _, allocation = prepare(client)
    key = issue_key(client, allocation["id"]).json()["pickup_key"]
    first = client.post("/delivery-api/v1/pickups/verify", json={"pickup_key": key})
    second = client.post("/delivery-api/v1/pickups/verify", json={"pickup_key": key})
    assert first.status_code == 200 and second.status_code == 200
    # 两个会话互不顶掉：多标签页同时取件是正常用法
    for response in (first, second):
        token = response.json()["session_token"]
        read = client.get(
            f"/delivery-api/v1/pickups/{allocation['id']}/mailbox",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert read.status_code == 200

    code = client.post(
        "/delivery-api/v1/pickups",
        headers=HEADERS,
        json={"allocation_id": allocation["id"]},
    ).json()["pickup_code"]
    assert (
        client.post("/delivery-api/v1/pickups/verify", json={"pickup_code": code}).status_code
        == 200
    )
    assert (
        client.post("/delivery-api/v1/pickups/verify", json={"pickup_code": code}).status_code
        == 401
    )


def test_reissue_invalidates_the_previous_key(client):
    _, allocation = prepare(client)
    old_key = issue_key(client, allocation["id"]).json()["pickup_key"]
    assert (
        client.post("/delivery-api/v1/pickups/verify", json={"pickup_key": old_key}).status_code
        == 200
    )
    new_key = issue_key(client, allocation["id"], reissue=True).json()["pickup_key"]
    assert new_key != old_key
    assert (
        client.post("/delivery-api/v1/pickups/verify", json={"pickup_key": old_key}).status_code
        == 401
    )
    assert (
        client.post("/delivery-api/v1/pickups/verify", json={"pickup_key": new_key}).status_code
        == 200
    )


def test_key_stored_as_digest_only(client):
    _, allocation = prepare(client)
    key = issue_key(client, allocation["id"]).json()["pickup_key"]
    with Session(client.app.state.engine) as db:
        stored = db.scalars(select(PickupCredential.key_hash)).all()
    assert hash_secret(key) in stored
    assert all(value != key for value in stored if value)


def test_email_mismatch_is_rejected(client):
    _, allocation = prepare(client, address="match@example.test")
    key = issue_key(client, allocation["id"]).json()["pickup_key"]
    good = client.post(
        "/delivery-api/v1/pickups/verify",
        json={"pickup_key": key, "email": "MATCH@example.test"},
    )
    assert good.status_code == 200
    bad = client.post(
        "/delivery-api/v1/pickups/verify",
        json={"pickup_key": key, "email": "other@example.test"},
    )
    assert bad.status_code == 401
    assert bad.json()["detail"]["code"] == "DELIVERY_PICKUP_INVALID"


def test_verify_requires_a_credential(client):
    response = client.post("/delivery-api/v1/pickups/verify", json={"email": "x@example.test"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "DELIVERY_PICKUP_CREDENTIAL_REQUIRED"
    malformed = client.post(
        "/delivery-api/v1/pickups/verify", json={"pickup_key": "not-a-key"}
    )
    assert malformed.status_code == 401


def test_expired_allocation_cannot_issue_or_use_a_key(client):
    _, allocation = prepare(client, ttl_seconds=0)
    assert issue_key(client, allocation["id"]).status_code == 409
    assert (
        client.post(
            "/delivery-api/v1/pickups/verify", json={"pickup_key": "tok_" + "a" * 64}
        ).status_code
        == 401
    )


@pytest.mark.parametrize("path", ["/pickups/verify"])
def test_public_routes_send_hardening_headers(make_client, path):
    with make_client() as client:
        response = client.post(f"/delivery-api/v1{path}", json={"pickup_key": "tok_" + "b" * 64})
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "default-src 'none'" in response.headers["content-security-policy"]


def test_failures_are_recorded_in_both_value_and_source_buckets(make_client):
    with make_client(pickup_failure_limit=2, pickup_backoff_seconds=300) as client:
        guess = "tok_" + "c" * 64
        first = client.post("/delivery-api/v1/pickups/verify", json={"pickup_key": guess})
        assert first.status_code == 401
        second = client.post("/delivery-api/v1/pickups/verify", json={"pickup_key": guess})
        assert second.status_code == 401
        third = client.post("/delivery-api/v1/pickups/verify", json={"pickup_key": guess})
        assert third.status_code == 429
        assert third.json()["detail"]["code"] == "DELIVERY_PICKUP_RETRY_LATER"

        with Session(client.app.state.engine) as db:
            buckets = {row.source_hash for row in db.scalars(select(PickupAttempt)).all()}
        # 值桶：同一串猜测被单独计数，换 IP 也不能无成本重试
        assert hash_secret(f"pickup-value:{hash_secret(guess)}") in buckets
        # IP 桶：单来源整体限速（TestClient 的来源主机固定为 testclient）
        assert hash_secret("pickup-source:testclient") in buckets


def test_successful_pickup_clears_own_value_and_source_buckets(make_client):
    """成功取件清掉「本次提交值」与「来源 IP」两个桶，但不放过别人猜的其它值。"""
    with make_client(pickup_failure_limit=5) as client:
        _, allocation = prepare(client)
        key = issue_key(client, allocation["id"]).json()["pickup_key"]
        wrong = "tok_" + "d" * 64
        assert (
            client.post("/delivery-api/v1/pickups/verify", json={"pickup_key": wrong}).status_code
            == 401
        )
        assert (
            client.post("/delivery-api/v1/pickups/verify", json={"pickup_key": key}).status_code
            == 200
        )

        with Session(client.app.state.engine) as db:
            buckets = {
                row.source_hash: row.failed_attempts
                for row in db.scalars(select(PickupAttempt)).all()
            }
        assert buckets[hash_secret("pickup-source:testclient")] == 0
        # 别人猜错的那串值仍然记着，不能因为一次成功就把它抹掉
        assert buckets[hash_secret(f"pickup-value:{hash_secret(wrong)}")] == 1
