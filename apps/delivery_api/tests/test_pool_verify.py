"""E4：对外读信 / 取码（OEP 契约：messages / verification-code / verification-link / wait-message）。

锁的点：
- 信封与 OEP 一致（成功 ``success:true``、失败顶格 ``code``）；没码 404 ``EMAIL_NOT_FOUND``。
- ``/api/external/*`` 别名与 ``/pool-api/v1/*`` 等价（注册机按 OEP 全路径配 base_url 也能用）。
- 读信前兜底刷上游不炸；wait-message 按 baseline 轮询，超时 404。
"""

from test_pool_claim import ADMIN, bearer, create_key, seed_mailbox

from delivery_api.models import utc_now


def _seed(client, address: str, messages: list[dict], **mailbox_fields) -> str:
    """建一个带邮件的池邮箱。messages 走 inventory 端点的入库规范（与现网一致）。"""
    created = client.post(
        "/delivery-api/v1/inventory/mailboxes",
        headers=ADMIN,
        json={"address": address, "source_label": "outlook_plus", "messages": messages, **mailbox_fields},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _code_msg(code: str = "123456", subject: str = "你的验证码是 123456") -> dict:
    return {
        "sender": "verify@plus.test",
        "recipient": "box@plus.test",
        "subject": subject,
        "body_text": "请在页面输入验证码完成注册。",
        "received_at": utc_now().isoformat(),
        "otp_code": code,
    }


def _link_msg(link: str = "https://plus.test/act?t=abc") -> dict:
    return {
        "sender": "noreply@plus.test",
        "recipient": "box@plus.test",
        "subject": "点击链接完成注册",
        "body_text": f"请点击链接：{link}",
        "received_at": utc_now().isoformat(),
        "link": link,
    }


# ------------------------------------------------------------------ verification


def test_verification_code_returns_code_with_formatted(client):
    raw = create_key(client)["api_key"]
    _seed(client, "box@plus.test", [_code_msg()])

    body = client.get(
        "/pool-api/v1/verification-code", headers=bearer(raw), params={"email": "box@plus.test"}
    ).json()
    assert body["success"] is True
    data = body["data"]
    assert data["verification_code"] == "123456"
    assert data["formatted"] == "123456"
    assert data["verification_link"] is None
    assert data["email"] == "box@plus.test"
    assert data["subject"].startswith("你的验证码")
    assert data["matched_email_id"]


def test_verification_code_not_found_is_404_email_not_found(client):
    raw = create_key(client)["api_key"]
    _seed(client, "empty@plus.test", [])

    body = client.get(
        "/pool-api/v1/verification-code", headers=bearer(raw), params={"email": "empty@plus.test"}
    )
    assert body.status_code == 404
    assert body.json()["code"] == "EMAIL_NOT_FOUND"
    assert body.json()["success"] is False


def test_verification_link_returns_link_and_nulls_code(client):
    raw = create_key(client)["api_key"]
    _seed(client, "link@plus.test", [_link_msg()])

    body = client.get(
        "/pool-api/v1/verification-link", headers=bearer(raw), params={"email": "link@plus.test"}
    ).json()
    assert body["success"] is True
    assert body["data"]["verification_link"] == "https://plus.test/act?t=abc"
    assert body["data"]["formatted"] == "https://plus.test/act?t=abc"
    assert body["data"]["verification_code"] is None


def test_unknown_mailbox_is_email_not_found(client):
    raw = create_key(client)["api_key"]
    body = client.get("/pool-api/v1/verification-code", headers=bearer(raw), params={"email": "nobody@x.test"})
    assert body.status_code == 404 and body.json()["code"] == "EMAIL_NOT_FOUND"


# ------------------------------------------------------------------ messages


def test_messages_lists_newest_first_with_count_and_has_more(client):
    raw = create_key(client)["api_key"]
    _seed(client, "box@plus.test", [_code_msg("1", "m1"), _code_msg("2", "m2")])

    body = client.get("/pool-api/v1/messages", headers=bearer(raw), params={"email": "box@plus.test"}).json()
    assert body["success"] is True
    data = body["data"]
    assert data["count"] == 2
    assert data["has_more"] is False
    subjects = [item["subject"] for item in data["emails"]]
    assert subjects == ["m2", "m1"]  # 最新在前
    assert data["emails"][0]["verification_code"] == "2"
    assert data["emails"][0]["email_address"] == "box@plus.test"


def test_messages_pagination_and_filters(client):
    raw = create_key(client)["api_key"]
    _seed(client, "box@plus.test", [_code_msg("1", "m1"), _code_msg("2", "m2"), _code_msg("3", "m3")])

    page = client.get(
        "/pool-api/v1/messages", headers=bearer(raw),
        params={"email": "box@plus.test", "top": 2, "skip": 0},
    ).json()["data"]
    assert [i["subject"] for i in page["emails"]] == ["m3", "m2"]
    assert page["has_more"] is True

    subject = client.get(
        "/pool-api/v1/messages", headers=bearer(raw),
        params={"email": "box@plus.test", "subject_contains": "m1"},
    ).json()["data"]
    assert [i["subject"] for i in subject["emails"]] == ["m1"]

    bad = client.get(
        "/pool-api/v1/messages", headers=bearer(raw), params={"email": "box@plus.test", "top": 0}
    )
    assert bad.status_code == 422  # Query ge=1 挡在业务层外


# ------------------------------------------------------------------ wait-message


def test_wait_message_returns_an_eligible_message_immediately(client):
    raw = create_key(client)["api_key"]
    _seed(client, "box@plus.test", [_code_msg("777777", "刚刚到")])

    body = client.get(
        "/pool-api/v1/wait-message", headers=bearer(raw),
        params={"email": "box@plus.test", "timeout_seconds": 10, "poll_interval": 1},
    )
    assert body.status_code == 200
    data = body.json()["data"]
    assert data["verification_code"] == "777777"
    assert data["subject"] == "刚刚到"


def test_wait_message_times_out_to_email_not_found(client):
    raw = create_key(client)["api_key"]
    _seed(client, "silent@plus.test", [])

    body = client.get(
        "/pool-api/v1/wait-message", headers=bearer(raw),
        params={"email": "silent@plus.test", "timeout_seconds": 2, "poll_interval": 0.5},
    )
    assert body.status_code == 404
    assert body.json()["code"] == "EMAIL_NOT_FOUND"


def test_wait_message_async_creates_probe_and_finds(client):
    raw = create_key(client)["api_key"]
    _seed(client, "box@plus.test", [_code_msg("666001", "异步到")])

    created = client.get(
        "/pool-api/v1/wait-message", headers=bearer(raw),
        params={"email": "box@plus.test", "mode": "async", "timeout_seconds": 30},
    )
    assert created.status_code == 202
    probe_id = created.json()["data"]["probe_id"]
    assert probe_id

    # 消息已在基线内（建探测前 2s 宽限），一次轮询即命中。
    found = client.get(f"/pool-api/v1/probe/{probe_id}", headers=bearer(raw)).json()
    assert found["success"] is True and found["data"]["status"] == "found"
    assert found["data"]["message"]["verification_code"] == "666001"


def test_probe_times_out_when_no_eligible_message(client):
    import time as t
    raw = create_key(client)["api_key"]
    _seed(client, "silent@plus.test", [])

    created = client.get(
        "/pool-api/v1/wait-message", headers=bearer(raw),
        params={"email": "silent@plus.test", "mode": "async", "timeout_seconds": 1},
    )
    assert created.status_code == 202
    probe_id = created.json()["data"]["probe_id"]

    t.sleep(1.3)  # 等它过 timeout
    body = client.get(f"/pool-api/v1/probe/{probe_id}", headers=bearer(raw))
    assert body.status_code == 404
    assert body.json()["code"] == "EMAIL_NOT_FOUND"

    from sqlalchemy.orm import Session

    from delivery_api.models import PoolProbe
    with Session(client.app.state.engine) as db:
        probe = db.get(PoolProbe, probe_id)
        assert probe is not None and probe.status == "timeout"


def test_probe_unknown_is_404(client):
    raw = create_key(client)["api_key"]
    body = client.get("/pool-api/v1/probe/nope", headers=bearer(raw))
    assert body.status_code == 404 and body.json()["code"] == "PROBE_NOT_FOUND"


def test_oep_external_probe_alias_works(client):
    raw = create_key(client)["api_key"]
    _seed(client, "box@plus.test", [_code_msg("777123", "别名")])
    created = client.get(
        "/api/external/wait-message", headers=bearer(raw),
        params={"email": "box@plus.test", "mode": "async", "timeout_seconds": 30},
    )
    assert created.status_code == 202
    probe_id = created.json()["data"]["probe_id"]
    found = client.get(f"/api/external/probe/{probe_id}", headers=bearer(raw)).json()
    assert found["data"]["status"] == "found"
    assert found["data"]["message"]["verification_code"] == "777123"


# ------------------------------------------------------------------ OEP 别名等价


def test_oep_external_aliases_are_equivalent_to_pool_paths(client):
    raw = create_key(client)["api_key"]
    _seed(client, "box@plus.test", [_code_msg("555555", "别名")])

    via_pool = client.get(
        "/pool-api/v1/verification-code", headers=bearer(raw), params={"email": "box@plus.test"}
    ).json()
    via_external = client.get(
        "/api/external/verification-code", headers=bearer(raw), params={"email": "box@plus.test"}
    ).json()
    assert via_pool == via_external

    # 池端点别名：claim / stats 同一条 handler。
    claimed = client.post(
        "/api/external/pool/claim-random",
        headers=bearer(raw),
        json={"caller_id": "caller-1", "task_id": "task-1", "project_key": "proj-a"},
    )
    assert claimed.status_code == 200 and claimed.json()["success"] is True
    stats = client.get("/api/external/pool/stats", headers=bearer(raw)).json()
    assert stats["success"] is True and "pool_counts" in stats["data"]


def test_read_endpoints_require_a_pool_key(client):
    seed_mailbox(client, address="anon@plus.test")
    assert client.get("/pool-api/v1/messages", params={"email": "anon@plus.test"}).status_code == 401
    assert client.get("/api/external/verification-code", params={"email": "anon@plus.test"}).status_code == 401