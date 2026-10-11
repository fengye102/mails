"""C 批量签发测试：去重、逐项失败、部分成功、一行发货文本。"""

OPERATOR = "operator-test-token"
HEADERS = {"Authorization": f"Bearer {OPERATOR}"}


def add_mailbox(client, address):
    return client.post(
        "/delivery-api/v1/inventory/mailboxes",
        headers=HEADERS,
        json={"address": address, "source_label": "outlook_plus"},
    ).json()["id"]


def batch(client, **payload):
    body = {"external_ref": "order-batch", "ttl_seconds": 600}
    body.update(payload)
    return client.post(
        "/delivery-api/v1/inventory/allocations/batch", headers=HEADERS, json=body
    )


def test_batch_issues_one_key_per_mailbox(client):
    first = add_mailbox(client, "batch-1@example.test")
    second = add_mailbox(client, "batch-2@example.test")
    response = batch(client, mailbox_ids=[first, second], issue_key=True)
    assert response.status_code == 201
    payload = response.json()
    assert payload["count"] == 2
    assert payload["failures"] == []
    addresses = sorted(item["address"] for item in payload["deliveries"])
    assert addresses == ["batch-1@example.test", "batch-2@example.test"]
    for item in payload["deliveries"]:
        assert item["pickup_key"].startswith("tok_")
        assert item["pickup_url"].startswith("/pickup#")
        assert item["delivery_text"] == (
            f"{item['address']}---{item['pickup_key']}---{item['pickup_url']}"
        )
    assert {item["external_ref"] for item in payload["deliveries"]} == {
        "order-batch-001",
        "order-batch-002",
    }


def test_batch_accepts_allocations_without_keys(client):
    target = add_mailbox(client, "batch-plain@example.test")
    payload = batch(client, mailbox_ids=[target]).json()
    assert payload["count"] == 1
    assert "pickup_key" not in payload["deliveries"][0]


def test_batch_deduplicates_and_reports_partial_failures(client):
    target = add_mailbox(client, "batch-dup@example.test")
    response = batch(
        client,
        mailbox_ids=[target, target, "00000000-0000-0000-0000-000000000000"],
        issue_key=True,
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["count"] == 1
    assert len(payload["failures"]) == 1
    assert payload["failures"][0]["mailbox_id"] == "00000000-0000-0000-0000-000000000000"


def test_batch_fails_when_no_mailbox_can_be_issued(client):
    response = batch(
        client,
        mailbox_ids=["00000000-0000-0000-0000-000000000000"],
        issue_key=True,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DELIVERY_MAILBOX_NOT_AVAILABLE"


def test_batch_limits_and_operator_auth(client):
    target = add_mailbox(client, "batch-limit@example.test")
    assert batch(client, mailbox_ids=[]).status_code == 422
    assert batch(client, mailbox_ids=[f"id-{index}" for index in range(751)]).status_code == 422
    unauthorized = client.post(
        "/delivery-api/v1/inventory/allocations/batch",
        json={"mailbox_ids": [target], "issue_key": True},
    )
    assert unauthorized.status_code == 401


def test_batch_key_can_be_used_for_pickup(client):
    target = add_mailbox(client, "batch-pickup@example.test")
    delivery = batch(client, mailbox_ids=[target], issue_key=True).json()["deliveries"][0]
    verified = client.post(
        "/delivery-api/v1/pickups/verify",
        json={"pickup_key": delivery["pickup_key"], "email": delivery["address"]},
    )
    assert verified.status_code == 200
    session = verified.json()
    read = client.get(
        f"/delivery-api/v1/pickups/{session['allocation_id']}/mailbox",
        headers={"Authorization": f"Bearer {session['session_token']}"},
    )
    assert read.status_code == 200
    assert read.json()["address"] == "batch-pickup@example.test"
