"""A 站 icloud_hme（iCloud 隐藏邮箱引擎）只读来源：适配器契约 + 全链路接线。

分两层测，因为坏在这两处线上表现完全不同：

- **适配器层**（假上游 MockTransport）：请求形状、字段映射、错误码映射、凭据不外泄。
  形状错了是「读不到信」，凭据泄了是事故。
- **接线层**（TestClient + 真库）：连接登记 → 邮箱登记 → 实时读信 → 定时同步，
  四条路必须走同一个寻址规则；「登记时按 A 存、同步时按 B 读」会静默读成空信箱。
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from mails_api.app import create_app
from mails_api.models import Base, Mailbox, ProviderConnection, User
from mails_api.providers.base import MessageListResult, MessageSummary, ProviderError
from mails_api.providers.icloud_hme import (
    ICloudHMEAdapter,
    mailbox_key,
    parse_mailbox_key,
)
from mails_api.security.crypto import encrypt_secret
from mails_api.security.passwords import hash_password
from mails_api.services.sync import sync_mailbox
from mails_api.settings import Settings

KEY = bytes(range(32))
ORIGIN = "https://engine.example"
TOKEN = "operator-token-abc"
ACCOUNT = "acct-1"
ALIAS = "alias@icloud.com"
MAILBOX = mailbox_key(ACCOUNT, ALIAS)
NOW = datetime(2026, 10, 12, 7, 0, tzinfo=UTC)
MAIL_PATH = f"/icloud-api/v1/accounts/{ACCOUNT}/mail"

# 字段形状照 apps/icloud_api/src/icloud_api/mail/imap_client.py 的 Message.as_dict()。
MESSAGE = {
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


def envelope(messages=None, address=ALIAS, account_id=ACCOUNT) -> dict:
    items = list([MESSAGE] if messages is None else messages)
    return {
        "account_id": account_id,
        "address": address,
        "count": len(items),
        "messages": items,
        "method": "imap",
    }


def adapter_for(handler, seen: list | None = None) -> ICloudHMEAdapter:
    def upstream(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    return ICloudHMEAdapter(httpx.AsyncClient(transport=httpx.MockTransport(upstream)))


def build_client(engine, upstream, origins=(ORIGIN,)) -> tuple[Settings, TestClient]:
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(username="owner", password_hash=hash_password("secret")))
        db.commit()
    settings = Settings(
        database_url="sqlite://",
        encryption_key=base64.b64encode(KEY).decode(),
        public_origin="http://localhost:5173",
        upstream_origins=list(origins),
    )
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return settings, TestClient(create_app(settings, engine, async_client))


def login(client: TestClient, settings: Settings) -> dict:
    response = client.post(
        "/api/v1/session",
        headers={"Origin": settings.public_origin},
        json={"username": "owner", "password": "secret"},
    )
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def engine_upstream(request: httpx.Request) -> httpx.Response:
    """引擎的最小假上游：capabilities + 读信。"""
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    if request.url.path == "/icloud-api/v1/capabilities":
        return httpx.Response(200, json={"service": "icloud-hme-engine", "features": {}})
    if request.url.path == MAIL_PATH:
        return httpx.Response(200, json=envelope())
    raise AssertionError(f"unexpected upstream request: {request.method} {request.url}")


# ---- mailbox key（寻址的唯一真相）----


def test_mailbox_key_roundtrip():
    key = mailbox_key(ACCOUNT, "Alias@iCloud.com")
    assert key.startswith("icloud_hme:")
    assert parse_mailbox_key(key) == (ACCOUNT, ALIAS)
    # 编码后的 key 里不能出现原始地址（key 会落库、出现在接口响应里）。
    assert ALIAS not in key


@pytest.mark.parametrize(
    "raw",
    [
        "cymail:YWNjdA:YWxpYXM",  # 别的来源的 key
        "icloud_hme:onlyone",  # 少一段
        "icloud_hme::",  # 空段
        "icloud_hme:!!!:!!!",  # 不是 base64
        "icloud_hme:",  # 只有前缀
        "",
        123,
    ],
)
def test_parse_mailbox_key_rejects_malformed_values(raw):
    with pytest.raises(ValueError):
        parse_mailbox_key(raw)


@pytest.mark.parametrize("bad_account", ["", "   ", "acct/1", "acct\\1", "acct\x00", "a" * 300])
def test_mailbox_key_rejects_path_hostile_account_ids(bad_account):
    # 账号 id 会拼进 URL 路径，斜杠/控制字符必须在编码前挡住。
    with pytest.raises(ValueError):
        mailbox_key(bad_account, ALIAS)


# ---- probe ----


@pytest.mark.asyncio
async def test_probe_sends_bearer_token_to_capabilities():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/icloud-api/v1/capabilities"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json={"service": "icloud-hme-engine", "features": {}})

    await adapter_for(handler, seen).probe(ORIGIN, TOKEN)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_probe_rejects_foreign_service_without_leaking_token():
    adapter = adapter_for(lambda request: httpx.Response(200, json={"service": "not-the-engine"}))
    with pytest.raises(ProviderError) as excinfo:
        await adapter.probe(ORIGIN, TOKEN)
    assert excinfo.value.code == "UPSTREAM_CONTRACT"
    assert TOKEN not in str(excinfo.value)


# ---- 错误码映射 ----


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "UPSTREAM_AUTH"),
        (403, "UPSTREAM_AUTH"),
        (400, "UPSTREAM_CONTRACT"),  # 账号没配 App Password
        (404, "UPSTREAM_CONTRACT"),  # 账号不存在
        (429, "UPSTREAM_CONTRACT"),  # 引擎侧限速
    ],
)
async def test_upstream_status_maps_to_fixed_error_codes(status, code):
    adapter = adapter_for(lambda request: httpx.Response(status, json={}))
    with pytest.raises(ProviderError) as excinfo:
        await adapter.list_messages(ORIGIN, TOKEN, MAILBOX)
    assert excinfo.value.code == code
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in repr(excinfo.value.summary)


@pytest.mark.asyncio
async def test_upstream_5xx_is_retried_once_then_reported_unavailable():
    seen: list[httpx.Request] = []
    adapter = adapter_for(lambda request: httpx.Response(502, json={}), seen)
    with pytest.raises(ProviderError) as excinfo:
        await adapter.list_messages(ORIGIN, TOKEN, MAILBOX)
    assert excinfo.value.code == "UPSTREAM_UNAVAILABLE"
    assert len(seen) == 2, "5xx 要重试一次再放弃"


@pytest.mark.asyncio
async def test_upstream_timeout_is_retried_once_then_reported_timeout():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    adapter = adapter_for(handler, seen)
    with pytest.raises(ProviderError) as excinfo:
        await adapter.list_messages(ORIGIN, TOKEN, MAILBOX)
    assert excinfo.value.code == "UPSTREAM_TIMEOUT"
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_non_json_body_is_a_contract_error():
    adapter = adapter_for(lambda request: httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(ProviderError) as excinfo:
        await adapter.list_messages(ORIGIN, TOKEN, MAILBOX)
    assert excinfo.value.code == "UPSTREAM_CONTRACT"


# ---- 读信映射 ----


@pytest.mark.asyncio
async def test_list_messages_maps_engine_fields_and_keeps_method_stable():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == MAIL_PATH
        assert request.url.params["address"] == ALIAS
        assert request.url.params["limit"] == "100"
        assert request.url.params["days"] == "7"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json=envelope())

    result = await adapter_for(handler).list_messages(ORIGIN, TOKEN, MAILBOX)

    assert result.issues == []
    (summary,) = result.summaries
    assert summary.provider_id == "4812"
    # method 固定为 imap：照抄引擎的 method 会让同一封邮件在
    # imap / imap_recipient_search 之间变出两个 provider key（归档重复）。
    assert summary.method == "imap"
    assert summary.provider_message_key == "imap:4812"
    assert summary.sender == "no-reply@icloud.com"
    assert summary.subject == "Your code"
    assert summary.snippet == "Verification code 123456"
    assert summary.received_at == datetime(2026, 10, 12, 6, 32, 10, tzinfo=UTC)


@pytest.mark.asyncio
async def test_bad_messages_become_issues_without_sinking_the_batch():
    no_id = {"id": "", "subject": "x"}
    no_date = {**MESSAGE, "id": "4813", "date": ""}
    adapter = adapter_for(
        lambda request: httpx.Response(200, json=envelope([no_id, no_date, MESSAGE]))
    )

    result = await adapter.list_messages(ORIGIN, TOKEN, MAILBOX)

    assert [issue.absolute_index for issue in result.issues] == [0, 1]
    assert {issue.code for issue in result.issues} == {"UPSTREAM_CONTRACT"}
    assert [summary.provider_id for summary in result.summaries] == ["4812"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        envelope(address="someone-else@icloud.com"),
        envelope(account_id="acct-2"),
        {"account_id": ACCOUNT, "address": ALIAS, "messages": "not-a-list"},
        ["not", "a", "dict"],
    ],
)
async def test_broken_envelope_is_a_contract_error(payload):
    # 回显不一致 / 形状不对时必须整体报错：把别人的邮件归档进来比读不到更糟。
    adapter = adapter_for(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ProviderError) as excinfo:
        await adapter.list_messages(ORIGIN, TOKEN, MAILBOX)
    assert excinfo.value.code == "UPSTREAM_CONTRACT"


@pytest.mark.asyncio
async def test_check_mailbox_verifies_readability_before_registration():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == MAIL_PATH
        assert request.url.params["address"] == ALIAS
        assert request.url.params["limit"] == "1"
        return httpx.Response(200, json=envelope())

    assert await adapter_for(handler).check_mailbox(ORIGIN, TOKEN, MAILBOX) == MAILBOX


@pytest.mark.asyncio
async def test_check_mailbox_reports_unreadable_account_as_contract_error():
    adapter = adapter_for(lambda request: httpx.Response(400, json={"detail": "未设置 App 专用密码"}))
    with pytest.raises(ProviderError) as excinfo:
        await adapter.check_mailbox(ORIGIN, TOKEN, MAILBOX)
    assert excinfo.value.code == "UPSTREAM_CONTRACT"


@pytest.mark.asyncio
async def test_get_message_reuses_the_body_from_the_list_call():
    seen: list[httpx.Request] = []
    adapter = adapter_for(lambda request: httpx.Response(200, json=envelope()), seen)
    result = await adapter.list_messages(ORIGIN, TOKEN, MAILBOX)

    detail = await adapter.get_message(ORIGIN, TOKEN, MAILBOX, result.summaries[0])

    assert detail.body_text == MESSAGE["body_text"]
    assert detail.recipient == ALIAS
    assert detail.rfc_message_id == "<abc@example.com>"
    assert len(seen) == 1, "正文已在列表阶段拿到，详情不该再打一次上游"


@pytest.mark.asyncio
async def test_get_message_relists_when_the_cache_is_cold():
    seen: list[httpx.Request] = []
    adapter = adapter_for(lambda request: httpx.Response(200, json=envelope()), seen)
    summary = MessageSummary("4812", "imap", "no-reply@icloud.com", "Your code", "snippet", NOW)

    detail = await adapter.get_message(ORIGIN, TOKEN, MAILBOX, summary)

    assert detail.body_text == MESSAGE["body_text"]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_empty_body_falls_back_to_snippet_instead_of_faking_content():
    adapter = adapter_for(lambda request: httpx.Response(200, json=envelope([{**MESSAGE, "body_text": ""}])))
    result = await adapter.list_messages(ORIGIN, TOKEN, MAILBOX)

    detail = await adapter.get_message(ORIGIN, TOKEN, MAILBOX, result.summaries[0])

    assert detail.body_text == MESSAGE["snippet"]


@pytest.mark.asyncio
async def test_get_message_rejects_foreign_summary():
    adapter = adapter_for(lambda request: httpx.Response(200, json=envelope()))
    foreign = MessageSummary("4812", "web_api", "s", "s", "s", NOW)
    with pytest.raises(ProviderError) as excinfo:
        await adapter.get_message(ORIGIN, TOKEN, MAILBOX, foreign)
    assert excinfo.value.code == "UPSTREAM_CONTRACT"


# ---- 接线：连接登记 / 邮箱登记 ----


def test_connection_and_mailbox_registration_use_bearer_and_encoded_key(engine):
    settings, client = build_client(engine, engine_upstream)
    with client:
        csrf = login(client, settings)
        created = client.post(
            "/api/v1/connections",
            headers=csrf,
            json={
                "kind": "icloud_hme",
                "name": "icloud-engine",
                "base_url": ORIGIN,
                "api_key": TOKEN,
                "config": {"account_id": ACCOUNT},
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["config"] == {"account_id": ACCOUNT}

        mailbox = client.post(
            f"/api/v1/connections/{created.json()['id']}/mailboxes",
            headers=csrf,
            json={"address": ALIAS},
        )
        assert mailbox.status_code == 201, mailbox.text
        assert mailbox.json()["provider_mailbox_key"] == MAILBOX
        assert mailbox.json()["address"] == ALIAS

        assert TOKEN not in client.get("/api/v1/connections").text
        assert TOKEN not in client.get("/api/v1/mailboxes").text


def test_connection_without_account_id_is_rejected_before_upstream(engine):
    def upstream(request: httpx.Request) -> httpx.Response:
        raise AssertionError("配置非法时不该发出上游请求")

    settings, client = build_client(engine, upstream)
    with client:
        csrf = login(client, settings)
        response = client.post(
            "/api/v1/connections",
            headers=csrf,
            json={
                "kind": "icloud_hme",
                "name": "icloud-engine",
                "base_url": ORIGIN,
                "api_key": TOKEN,
                "config": {},
            },
        )
        # 校验错误统一被脱敏成 REQUEST_INVALID（不把内部错误码透给前端）。
        assert response.status_code == 422
        assert response.json()["detail"] == "REQUEST_INVALID"


def test_mailbox_registration_on_legacy_row_without_account_id_fails_loud(engine):
    """绕过 API 直接落库的老行（config 空）必须报固定错误码，而不是把裸地址当 key。"""
    settings, client = build_client(engine, engine_upstream)
    with Session(engine) as db:
        connection = ProviderConnection(
            kind="icloud_hme",
            name="legacy",
            base_url=ORIGIN,
            encrypted_secret=encrypt_secret(KEY, TOKEN),
            config_json={},
            status="active",
        )
        db.add(connection)
        db.commit()
        connection_id = connection.id

    with client:
        csrf = login(client, settings)
        response = client.post(
            f"/api/v1/connections/{connection_id}/mailboxes",
            headers=csrf,
            json={"address": ALIAS},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "ICLOUD_HME_ACCOUNT_ID_REQUIRED"


# ---- 接线：实时读信 ----


def test_live_mail_reads_through_the_engine(engine):
    settings, client = build_client(engine, engine_upstream)
    with client:
        csrf = login(client, settings)
        created = client.post(
            "/api/v1/connections",
            headers=csrf,
            json={
                "kind": "icloud_hme",
                "name": "icloud-engine",
                "base_url": ORIGIN,
                "api_key": TOKEN,
                "config": {"account_id": ACCOUNT},
            },
        )
        connection_id = created.json()["id"]

        response = client.get(
            f"/api/v1/connections/{connection_id}/live-mail", params={"address": ALIAS}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 1
        assert body["issues"] == []
        (item,) = body["items"]
        assert item["source_kind"] == "icloud_hme"
        assert item["provider_key"] == "imap:4812"
        assert item["sender"] == "no-reply@icloud.com"
        assert item["received_at"] == "2026-10-12T06:32:10+00:00"
        assert item["verification_code"] == "123456"
        assert TOKEN not in response.text


def test_live_mail_surfaces_upstream_auth_failure_as_502(engine):
    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/icloud-api/v1/capabilities":
            return httpx.Response(200, json={"service": "icloud-hme-engine"})
        return httpx.Response(401, json={"detail": "登录失败"})

    settings, client = build_client(engine, upstream)
    with client:
        csrf = login(client, settings)
        created = client.post(
            "/api/v1/connections",
            headers=csrf,
            json={
                "kind": "icloud_hme",
                "name": "icloud-engine",
                "base_url": ORIGIN,
                "api_key": TOKEN,
                "config": {"account_id": ACCOUNT},
            },
        )
        response = client.get(
            f"/api/v1/connections/{created.json()['id']}/live-mail", params={"address": ALIAS}
        )
        assert response.status_code == 502
        assert response.json()["detail"] == "UPSTREAM_AUTH"
        assert TOKEN not in response.text


# ---- 接线：定时同步 ----


@pytest.mark.asyncio
async def test_scheduled_sync_addresses_upstream_by_provider_key(engine):
    Base.metadata.create_all(engine)
    calls: list[str] = []

    class RecordingAdapter:
        async def list_messages(self, url, secret, address):
            calls.append(address)
            return MessageListResult([], [])

        async def get_message(self, *args):
            raise AssertionError("没有摘要时不该取详情")

    with Session(engine) as db:
        connection = ProviderConnection(
            kind="icloud_hme",
            name="icloud-engine",
            base_url=ORIGIN,
            encrypted_secret=encrypt_secret(KEY, TOKEN),
            config_json={"account_id": ACCOUNT},
            status="active",
        )
        db.add(connection)
        db.flush()
        item = Mailbox(
            connection_id=connection.id,
            provider_mailbox_key=MAILBOX,
            address=ALIAS,
            sync_state="idle",
        )
        db.add(item)
        db.commit()

        await sync_mailbox(db, item.id, RecordingAdapter(), NOW, encryption_key=KEY)

    assert calls == [MAILBOX], "定时同步必须用 provider key 寻址，而不是裸地址"
