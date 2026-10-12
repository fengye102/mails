"""取件读时的上游刷新：节流、失败降级、来源边界。

这层坏掉的后果最直接——取件人拿不到刚到的那封验证码邮件，或者反过来说
「已刷新」其实没刷（客户以为没收到信，实际是平台在骗他）。所以逐条钉住：

- 真刷一次并把新信入库（同一响应里就能看到）；
- 间隔内只刷一次（节流用条件 UPDATE，跨请求生效）；
- 上游挂了不打断取件：归档照给，状态如实回报 `unavailable`；
- 不是引擎来源的库存不刷（不假装刷过）；
- 关掉刷新 / 没配引擎时状态明确，不打上游。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from delivery_api.inventory_sync import DEFAULT_SOURCE_LABEL
from delivery_api.models import InventoryMailbox
from delivery_api.routes.pickups import _claim_refresh

ENGINE_URL = "https://engine.example"
TOKEN = "operator-token-abc"
ACCOUNT = "acct-1"
ALIAS = "alias@icloud.com"
OPERATOR = "operator-test-token"

ENGINE_MESSAGE = {
    "id": "4812",
    "sender": "no-reply@icloud.com",
    "to": ALIAS,
    "subject": "Your code",
    "date": "2026-10-12T06:32:10Z",
    "snippet": "Verification code 123456",
    "body_text": "Verification code 123456\n",
    "body_html": "<p>Verification code 123456</p>",
    "content_type": "text/plain",
    "rfc_message_id": "<abc@example.com>",
}


def mail_payload(messages: list[dict] | None = None, address: str = ALIAS) -> dict:
    items = [ENGINE_MESSAGE] if messages is None else messages
    return {
        "account_id": ACCOUNT,
        "address": address,
        "count": len(items),
        "messages": items,
        "method": "imap_recipient_search",
    }


def engine_client(calls: list, responder) -> httpx.Client:
    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responder(request)

    return httpx.Client(transport=httpx.MockTransport(upstream))


def prepare_delivery(client, *, address: str = ALIAS, source_label: str = DEFAULT_SOURCE_LABEL):
    """登记 → 分配 → 签发取件码 → 验证，返回 (邮箱, 分配, 会话令牌)。"""
    headers = {"Authorization": f"Bearer {OPERATOR}"}
    mailbox = client.post(
        "/delivery-api/v1/inventory/mailboxes",
        headers=headers,
        json={"address": address, "source_label": source_label, "messages": []},
    ).json()
    allocation = client.post(
        "/delivery-api/v1/inventory/allocations",
        headers=headers,
        json={"mailbox_id": mailbox["id"], "external_ref": "order-refresh", "ttl_seconds": 600},
    ).json()
    pickup = client.post(
        "/delivery-api/v1/pickups",
        headers=headers,
        json={"allocation_id": allocation["id"], "ttl_seconds": 300},
    ).json()
    verified = client.post(
        "/delivery-api/v1/pickups/verify", json={"pickup_code": pickup["pickup_code"]}
    ).json()
    return mailbox, allocation, verified["session_token"]


def read_mailbox(client, allocation_id: str, session_token: str):
    return client.get(
        f"/delivery-api/v1/pickups/{allocation_id}/mailbox",
        headers={"Authorization": f"Bearer {session_token}"},
    )


def make_engine_settings(make_client, calls, responder, **overrides):
    options = {
        "icloud_engine_url": ENGINE_URL,
        "icloud_engine_token": TOKEN,
        "icloud_engine_account_id": ACCOUNT,
    }
    options.update(overrides)
    client = make_client(**options)
    client.app.state.icloud_engine_client = engine_client(calls, responder)
    return client


def test_pickup_read_refreshes_and_shows_the_new_mail(make_client):
    calls: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.path == f"/icloud-api/v1/accounts/{ACCOUNT}/mail"
        assert request.url.params["address"] == ALIAS
        assert request.url.params["limit"] == "50"
        assert request.url.params["days"] == "7"
        return httpx.Response(200, json=mail_payload())

    with make_engine_settings(make_client, calls, responder) as client:
        _mailbox, allocation, token = prepare_delivery(client)

        response = read_mailbox(client, allocation["id"], token)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["sync"]["upstream"]["status"] == "refreshed"
        assert body["sync"]["upstream"]["inserted"] == 1
        assert body["sync"]["messages_total"] == 1
        # 新邮件在同一个响应里就能看到（先刷新再列信）。
        assert [item["verification_code"] for item in body["messages"]] == ["123456"]
        assert len(calls) == 1
        assert TOKEN not in response.text


def test_second_read_within_interval_is_throttled(make_client):
    calls: list[httpx.Request] = []
    responder = lambda request: httpx.Response(200, json=mail_payload())

    with make_engine_settings(make_client, calls, responder) as client:
        _mailbox, allocation, token = prepare_delivery(client)

        first = read_mailbox(client, allocation["id"], token)
        second = read_mailbox(client, allocation["id"], token)

        assert first.json()["sync"]["upstream"]["status"] == "refreshed"
        assert second.json()["sync"]["upstream"] == {
            "status": "throttled",
            "min_interval_seconds": 15,
        }
        assert len(calls) == 1, "15 秒内不该再打一次上游"
        # 节流期间仍然能读到已归档的邮件。
        assert second.json()["sync"]["messages_total"] == 1


def test_refresh_resumes_after_interval_and_stays_idempotent(make_client):
    calls: list[httpx.Request] = []
    responder = lambda request: httpx.Response(200, json=mail_payload())

    with make_engine_settings(make_client, calls, responder) as client:
        mailbox, allocation, token = prepare_delivery(client)
        read_mailbox(client, allocation["id"], token)

        # 把上次刷新时间推回过去，模拟「间隔已过」。
        with Session(client.app.state.engine) as db:
            row = db.get(InventoryMailbox, mailbox["id"])
            assert row is not None  # 刚登记过，必然存在
            row.last_refreshed_at = datetime(2020, 1, 1, tzinfo=UTC)
            db.commit()

        again = read_mailbox(client, allocation["id"], token)

        assert again.json()["sync"]["upstream"]["status"] == "refreshed"
        assert len(calls) == 2
        # 同一封邮件重复拉取只更新不新增。
        assert again.json()["sync"]["messages_total"] == 1
        assert again.json()["sync"]["upstream"]["updated"] == 1


def test_engine_failure_does_not_break_pickup(make_client):
    calls: list[httpx.Request] = []
    responder = lambda request: httpx.Response(401, json={"detail": "token 无效"})

    with make_engine_settings(make_client, calls, responder) as client:
        # 先用假上游塞一封历史邮件，再让它开始报错。
        _mailbox, allocation, token = prepare_delivery(client)
        calls.clear()

        response = read_mailbox(client, allocation["id"], token)

        assert response.status_code == 200, "上游故障不该让取件失败"
        body = response.json()
        assert body["sync"]["upstream"] == {"status": "unavailable", "code": "UPSTREAM_AUTH"}
        assert body["messages"] == []
        assert TOKEN not in response.text


def test_manual_source_mailboxes_are_not_refreshed(make_client):
    calls: list[httpx.Request] = []
    responder = lambda request: httpx.Response(200, json=mail_payload())

    with make_engine_settings(make_client, calls, responder) as client:
        _mailbox, allocation, token = prepare_delivery(client, source_label="manual")

        response = read_mailbox(client, allocation["id"], token)

        assert response.json()["sync"]["upstream"] == {"status": "not-applicable"}
        assert calls == []


def test_refresh_can_be_disabled_by_setting(make_client):
    calls: list[httpx.Request] = []
    responder = lambda request: httpx.Response(200, json=mail_payload())

    with make_engine_settings(
        make_client, calls, responder, pickup_refresh_min_interval_seconds=0
    ) as client:
        _mailbox, allocation, token = prepare_delivery(client)

        response = read_mailbox(client, allocation["id"], token)

        assert response.json()["sync"]["upstream"] == {"status": "disabled"}
        assert calls == []


def test_engine_without_credentials_reports_not_configured(make_client):
    calls: list[httpx.Request] = []
    responder = lambda request: httpx.Response(200, json=mail_payload())
    client = make_client(icloud_engine_source_label=DEFAULT_SOURCE_LABEL)
    client.app.state.icloud_engine_client = engine_client(calls, responder)

    with client:
        _mailbox, allocation, token = prepare_delivery(client)

        response = read_mailbox(client, allocation["id"], token)

        assert response.json()["sync"]["upstream"] == {"status": "not-configured"}
        assert calls == []


def test_claim_refresh_has_a_single_winner_per_interval(make_client):
    with make_client() as client, Session(client.app.state.engine) as db:
        row = InventoryMailbox(
            address=ALIAS, source_label=DEFAULT_SOURCE_LABEL, status="allocated"
        )
        db.add(row)
        db.commit()
        now = row.created_at

        assert _claim_refresh(db, row, now, 15) is True
        assert _claim_refresh(db, row, now, 15) is False, "同间隔内第二个请求不该拿到刷新权"
        assert _claim_refresh(db, row, now.replace(year=2020), 15) is False
        # 数据库里的时间戳只增不减：并发时后到的请求不会把时间戳改小。
        stored = db.scalar(select(InventoryMailbox.last_refreshed_at))
        assert stored is not None
