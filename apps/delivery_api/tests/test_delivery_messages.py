"""C 的邮件入库层测试：幂等、规范化（HTML→纯文本、验证码、行动链接）与取件可见性。"""

OPERATOR = "operator-test-token"
HEADERS = {"Authorization": f"Bearer {OPERATOR}"}


def register_mailbox(client, address="mailbox@example.test", messages=None):
    payload = {"address": address, "source_label": "outlook_plus"}
    if messages is not None:
        payload["messages"] = messages
    response = client.post(
        "/delivery-api/v1/inventory/mailboxes", headers=HEADERS, json=payload
    )
    assert response.status_code == 201
    return response.json()["id"]


def push(client, mailbox_id, messages):
    return client.post(
        f"/delivery-api/v1/inventory/mailboxes/{mailbox_id}/messages",
        headers=HEADERS,
        json={"messages": messages},
    )


def list_messages(client, mailbox_id, limit=None):
    suffix = f"?limit={limit}" if limit else ""
    response = client.get(
        f"/delivery-api/v1/inventory/mailboxes/{mailbox_id}/messages{suffix}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()


def test_registration_messages_are_normalized_into_plain_text(client):
    html = (
        "<html><body><p>验证码：&nbsp;654321</p>"
        '<p><a href="https://sso.openxlab.org.cn/active?token=abc">Activate</a></p>'
        "<script>alert(1)</script></body></html>"
    )
    mailbox_id = register_mailbox(
        client,
        messages=[
            {
                "id": "m-1",
                "sender": "noreply@openxlab.org.cn",
                "subject": "注册激活",
                "body_text": html,
                "received_at": "2026-10-11T02:00:00Z",
            }
        ],
    )
    payload = list_messages(client, mailbox_id)
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["provider_message_id"] == "m-1"
    assert item["otp_code"] == "654321"
    assert item["link"] == "https://sso.openxlab.org.cn/active?token=abc"
    assert "Activate (https://sso.openxlab.org.cn/active?token=abc)" in item["body_text"]
    assert "<" not in item["body_text"]
    assert "alert(1)" not in item["body_text"]


def test_push_is_idempotent_by_provider_message_id(client):
    mailbox_id = register_mailbox(client)
    first = push(
        client,
        mailbox_id,
        [
            {
                "provider_message_id": "upstream-1",
                "sender": "a@example.test",
                "subject": "第一次",
                "body_text": "Verification code: 123456",
                "received_at": "2026-10-11T01:00:00Z",
            }
        ],
    )
    assert first.status_code == 200
    assert first.json() == {"inserted": 1, "updated": 0, "total": 1}

    second = push(
        client,
        mailbox_id,
        [
            {
                "provider_message_id": "upstream-1",
                "sender": "a@example.test",
                "subject": "更新后的主题",
                "body_text": "Verification code: 999999",
                "received_at": "2026-10-11T01:00:00Z",
            }
        ],
    )
    # 同一封上游邮件重复推送只更新，不新增行（CYMail 的 ON CONFLICT DO UPDATE 语义）
    assert second.json() == {"inserted": 0, "updated": 1, "total": 1}
    payload = list_messages(client, mailbox_id)
    assert payload["total"] == 1
    assert payload["items"][0]["subject"] == "更新后的主题"
    assert payload["items"][0]["otp_code"] == "999999"


def test_missing_provider_message_id_uses_content_fingerprint(client):
    mailbox_id = register_mailbox(client)
    message = {
        "sender": "b@example.test",
        "subject": "指纹",
        "body_text": "hello",
        "received_at": "2026-10-11T03:00:00Z",
    }
    assert push(client, mailbox_id, [message]).json()["inserted"] == 1
    assert push(client, mailbox_id, [message]).json() == {
        "inserted": 0,
        "updated": 1,
        "total": 1,
    }
    changed = dict(message, subject="指纹变了")
    assert push(client, mailbox_id, [changed]).json()["inserted"] == 1
    assert list_messages(client, mailbox_id)["total"] == 2


def test_list_orders_newest_first_and_honours_limit(client):
    mailbox_id = register_mailbox(client)
    push(
        client,
        mailbox_id,
        [
            {
                "provider_message_id": f"m-{day}",
                "sender": "c@example.test",
                "subject": f"s-{day}",
                "body_text": "x",
                "received_at": f"2026-10-{day:02d}T00:00:00Z",
            }
            for day in (1, 2, 3)
        ],
    )
    payload = list_messages(client, mailbox_id)
    assert [item["subject"] for item in payload["items"]] == ["s-3", "s-2", "s-1"]
    limited = list_messages(client, mailbox_id, limit=2)
    assert limited["total"] == 3
    assert len(limited["items"]) == 2


def test_push_requires_operator_token_and_known_mailbox(client):
    mailbox_id = register_mailbox(client)
    assert (
        client.post(
            f"/delivery-api/v1/inventory/mailboxes/{mailbox_id}/messages",
            json={"messages": [{"subject": "x"}]},
        ).status_code
        == 401
    )
    missing = push(client, "00000000-0000-0000-0000-000000000000", [{"subject": "x"}])
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "DELIVERY_MAILBOX_NOT_FOUND"


def test_pickup_reads_ingested_messages_with_code_and_link(client):
    mailbox_id = register_mailbox(client, address="pickup-read@example.test")
    allocation = client.post(
        "/delivery-api/v1/inventory/allocations",
        headers=HEADERS,
        json={"mailbox_id": mailbox_id, "external_ref": "order-6", "ttl_seconds": 600},
    ).json()
    pickup = client.post(
        "/delivery-api/v1/pickups",
        headers=HEADERS,
        json={"allocation_id": allocation["id"]},
    ).json()
    push(
        client,
        mailbox_id,
        [
            {
                "provider_message_id": "activation-1",
                "sender": "noreply@openxlab.org.cn",
                "subject": "激活你的账号",
                "body_text": (
                    "<div>验证码：<b>482901</b></div>"
                    '<div><a href="https://sso.openxlab.org.cn/active?token=zz">点击激活</a></div>'
                ),
                "received_at": "2026-10-11T04:00:00Z",
            }
        ],
    )
    session = client.post(
        "/delivery-api/v1/pickups/verify", json={"pickup_code": pickup["pickup_code"]}
    ).json()
    response = client.get(
        f"/delivery-api/v1/pickups/{allocation['id']}/mailbox",
        headers={"Authorization": f"Bearer {session['session_token']}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sync"]["messages_total"] == 1
    assert body["sync"]["last_ingested_at"]
    assert "refreshed" not in body["sync"]
    message = body["messages"][0]
    assert set(message) == {
        "id",
        "sender",
        "subject",
        "received_at",
        "body_text",
        "verification_code",
        "link",
    }
    assert message["verification_code"] == "482901"
    assert message["link"] == "https://sso.openxlab.org.cn/active?token=zz"
    assert "<" not in message["body_text"]
    assert "点击激活 (https://sso.openxlab.org.cn/active?token=zz)" in message["body_text"]
