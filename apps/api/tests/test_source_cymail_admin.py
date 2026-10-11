"""CYMail 来源管理适配器与路由的真实契约测试。

写路径（创建/删除/停用/启用/库存同步）全部只跑 httpx.MockTransport 假响应，
不对线上 admin.orangeai.cfd 发起任何写请求。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from mails_api.app import create_app
from mails_api.models import Base, ProviderConnection, User
from mails_api.routes import source_cymail
from mails_api.security.crypto import encrypt_secret
from mails_api.security.passwords import hash_password
from mails_api.settings import Settings
from mails_api.sources_admin.base import SourceAdminError, SourceItem
from mails_api.sources_admin.cymail import CYMailSourceAdmin, build
from sqlalchemy.orm import Session

BASE_URL = "https://cy.example"
KEY = bytes(range(32))
SECRET = "cy-secret"
ACCOUNT = "acc_1"
ALIAS_ID = "anon-abc"


def envelope(data):
    return httpx.Response(200, json={"success": True, "data": data})


def alias(**overrides):
    return {
        "email": "user@icloud.com",
        "anonymousId": ALIAS_ID,
        "label": "注册某网站",
        "active": True,
        "createdAt": "2026-07-09T02:32:10Z",
        **overrides,
    }


# ---------------------------------------------------------------- 适配器单测


@pytest.mark.asyncio
async def test_probe_uses_api_key_and_success_envelope():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["x-api-key"] == SECRET
        assert request.url.path == "/api/accounts"
        return envelope([])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await build(client).probe(BASE_URL, SECRET)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_list_items_maps_aliases_to_source_items():
    def handler(request):
        assert request.headers["X-API-Key"] == SECRET
        assert request.url.path == "/api/aliases"
        assert request.url.params["account_id"] == ACCOUNT
        return envelope(
            {
                "account_id": ACCOUNT,
                "count": 2,
                "aliases": [
                    alias(),
                    alias(email="off@icloud.com", anonymousId="anon-2", active=False),
                ],
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await CYMailSourceAdmin(client).list_items(
            BASE_URL, SECRET, {"account_id": ACCOUNT}
        )

    assert all(isinstance(item, SourceItem) for item in items)
    assert items[0].provider_key == ALIAS_ID
    assert items[0].address == "user@icloud.com"
    assert items[0].status == "active"
    assert items[0].created_at == "2026-07-09T02:32:10Z"
    assert items[0].detail["label"] == "注册某网站"
    assert items[1].status == "inactive"


@pytest.mark.asyncio
async def test_create_item_posts_label_and_account_id():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/aliases"
        assert request.headers["X-API-Key"] == SECRET
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "email": "fresh@icloud.com",
                    "label": "GitHub",
                    "created_at": "2026-07-09T02:32:10Z",
                    "account_id": ACCOUNT,
                    "interface": "apple_account",
                    "active_count": 3,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        item = await CYMailSourceAdmin(client).create_item(
            BASE_URL, SECRET, {"label": "GitHub"}, {"account_id": ACCOUNT}
        )

    assert len(calls) == 1
    assert json.loads(calls[0].content) == {"account_id": ACCOUNT, "label": "GitHub"}
    assert item.provider_key == "fresh@icloud.com"
    assert item.address == "fresh@icloud.com"
    assert item.status == "active"
    assert item.detail["interface"] == "apple_account"


@pytest.mark.asyncio
async def test_delete_item_sends_account_id_body():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "DELETE"
        assert request.url.path == f"/api/aliases/{ALIAS_ID}"
        return envelope({"anonymous_id": ALIAS_ID})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await CYMailSourceAdmin(client).delete_item(
            BASE_URL, SECRET, ALIAS_ID, {"account_id": ACCOUNT}
        )

    assert len(calls) == 1
    assert json.loads(calls[0].content) == {"account_id": ACCOUNT}


@pytest.mark.asyncio
async def test_deactivate_reactivate_and_sync_inventory():
    paths = []

    def handler(request):
        paths.append((request.method, request.url.path))
        if request.url.path.endswith("/deactivate"):
            return envelope({"anonymous_id": ALIAS_ID, "success": True})
        if request.url.path.endswith("/reactivate"):
            return envelope({"anonymous_id": ALIAS_ID, "success": True})
        return envelope({"account_id": ACCOUNT, "synced": 7})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CYMailSourceAdmin(client)
        assert await adapter.deactivate_alias(
            BASE_URL, SECRET, ALIAS_ID, {"account_id": ACCOUNT}
        )
        assert await adapter.reactivate_alias(
            BASE_URL, SECRET, ALIAS_ID, {"account_id": ACCOUNT}
        )
        result = await adapter.sync_inventory(BASE_URL, SECRET, {"account_id": ACCOUNT})

    assert ("POST", f"/api/aliases/{ALIAS_ID}/deactivate") in paths
    assert ("POST", f"/api/aliases/{ALIAS_ID}/reactivate") in paths
    assert ("POST", "/api/inventory/sync") in paths
    assert result == {"account_id": ACCOUNT, "synced": 7}


@pytest.mark.asyncio
async def test_list_accounts_drops_credential_fields():
    def handler(request):
        return envelope(
            [
                {
                    "id": ACCOUNT,
                    "name": "主号",
                    "host": "imap.mail.me.com",
                    "status": "active",
                    "alias_total": 15,
                    "alias_active": 12,
                    "cookies": {"X-APPLE-WEBAUTH-TOKEN": "leak"},
                    "app_password": "abcd-efgh-ijkl-mnop",
                }
            ]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        accounts = await CYMailSourceAdmin(client).list_accounts(BASE_URL, SECRET)

    assert accounts == [
        {
            "id": ACCOUNT,
            "name": "主号",
            "host": "imap.mail.me.com",
            "status": "active",
            "alias_total": 15,
            "alias_active": 12,
        }
    ]
    assert "leak" not in str(accounts)
    assert "abcd-efgh-ijkl-mnop" not in str(accounts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {"success": False, "message": SECRET}, "SOURCE_AUTH"),
        (403, {"success": False, "message": SECRET}, "SOURCE_AUTH"),
        (404, {"success": False, "message": "账号不存在"}, "SOURCE_NOT_FOUND"),
        (409, {"success": False, "message": "总容量已满"}, "SOURCE_CONFLICT"),
        (400, {"success": False, "message": "参数错误"}, "SOURCE_REJECTED"),
        (502, {"success": False, "message": "iCloud 服务错误"}, "SOURCE_UNAVAILABLE"),
        (503, {"success": False, "message": SECRET}, "SOURCE_UNAVAILABLE"),
        (200, {"success": False, "message": SECRET}, "SOURCE_REJECTED"),
        (200, {"success": "yes", "data": []}, "SOURCE_CONTRACT"),
        (200, {"data": []}, "SOURCE_CONTRACT"),
        (200, {"success": True, "data": {"aliases": "nope"}}, "SOURCE_CONTRACT"),
    ],
)
async def test_error_mapping_is_fixed_and_never_leaks_secret(status, body, code):
    def handler(request):
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceAdminError) as raised:
            await CYMailSourceAdmin(client).list_items(
                BASE_URL, SECRET, {"account_id": ACCOUNT}
            )

    assert raised.value.code == code
    assert SECRET not in str(raised.value)


@pytest.mark.asyncio
async def test_timeout_maps_to_source_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceAdminError) as raised:
            await CYMailSourceAdmin(client).probe(BASE_URL, SECRET)

    assert raised.value.code == "SOURCE_TIMEOUT"


@pytest.mark.asyncio
async def test_missing_alias_field_is_contract_error():
    def handler(request):
        return envelope({"account_id": ACCOUNT, "aliases": [{"email": 1, "active": True}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceAdminError) as raised:
            await CYMailSourceAdmin(client).list_items(
                BASE_URL, SECRET, {"account_id": ACCOUNT}
            )

    assert raised.value.code == "SOURCE_CONTRACT"


@pytest.mark.asyncio
async def test_missing_account_id_config_is_contract_error():
    def handler(request):
        raise AssertionError("must not reach upstream without account_id")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceAdminError) as raised:
            await CYMailSourceAdmin(client).list_items(BASE_URL, SECRET, {})

    assert raised.value.code == "SOURCE_CONTRACT"


@pytest.mark.asyncio
async def test_provider_key_rejects_path_traversal():
    def handler(request):
        raise AssertionError("unsafe provider_key must not reach upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceAdminError) as raised:
            await CYMailSourceAdmin(client).delete_item(
                BASE_URL, SECRET, "../../etc/passwd", {"account_id": ACCOUNT}
            )

    assert raised.value.code == "SOURCE_CONTRACT"


@pytest.mark.asyncio
async def test_created_at_normalizes_offset_to_z():
    def handler(request):
        return envelope(
            {"account_id": ACCOUNT, "aliases": [alias(createdAt="2026-07-09T10:32:10+08:00")]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await CYMailSourceAdmin(client).list_items(
            BASE_URL, SECRET, {"account_id": ACCOUNT}
        )

    assert items[0].created_at == "2026-07-09T02:32:10Z"


# ---------------------------------------------------------------- 路由集成测试

UPSTREAM = {"mode": "ok", "requests": []}


def _upstream(request: httpx.Request) -> httpx.Response:
    UPSTREAM["requests"].append(request)
    assert request.headers["X-API-Key"] == SECRET
    mode = UPSTREAM["mode"]
    if mode == "auth":
        return httpx.Response(401, json={"success": False, "message": SECRET})
    if mode == "conflict":
        return httpx.Response(409, json={"success": False, "message": "总容量已满"})
    if mode == "timeout":
        raise httpx.ReadTimeout("slow", request=request)
    path, method = request.url.path, request.method
    if path == "/api/accounts":
        return envelope([])
    if path == "/api/aliases" and method == "GET":
        return envelope({"account_id": ACCOUNT, "count": 1, "aliases": [alias()]})
    if path == "/api/aliases" and method == "POST":
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "email": "fresh@icloud.com",
                    "label": "GitHub",
                    "created_at": "2026-07-09T02:32:10Z",
                    "account_id": ACCOUNT,
                    "interface": "icloud_web",
                },
            },
        )
    if path.startswith("/api/aliases/") and path.endswith("/deactivate"):
        return envelope({"anonymous_id": ALIAS_ID, "success": True})
    if path == f"/api/aliases/{ALIAS_ID}" and method == "DELETE":
        return envelope({"anonymous_id": ALIAS_ID})
    if path == "/api/inventory/sync":
        return envelope({"account_id": ACCOUNT, "synced": 3})
    raise AssertionError(f"unexpected upstream request: {method} {path}")


@pytest.fixture
def cymail_app(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(username="owner", password_hash=hash_password("secret"), role="owner"))
        db.add(User(username="member", password_hash=hash_password("secret"), role="member"))
        cymail = ProviderConnection(
            kind="cymail",
            name="CYMail",
            base_url=BASE_URL,
            encrypted_secret=encrypt_secret(KEY, SECRET),
            config_json={"account_id": ACCOUNT},
            status="active",
        )
        other = ProviderConnection(
            kind="outlook_plus",
            name="Outlook",
            base_url="https://mail.example",
            encrypted_secret=encrypt_secret(KEY, SECRET),
            config_json={},
            status="active",
        )
        db.add(cymail)
        db.add(other)
        db.commit()
        cymail_id, other_id = str(cymail.id), str(other.id)

    settings = Settings(
        database_url="sqlite://",
        encryption_key=base64.b64encode(KEY).decode(),
        public_origin="http://localhost:5173",
        upstream_origins=[BASE_URL],
    )
    UPSTREAM["mode"] = "ok"
    UPSTREAM["requests"] = []
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_upstream))
    app = create_app(settings, engine, upstream_client)
    app.include_router(source_cymail.router)
    with TestClient(app) as client:
        member_login = client.post(
            "/api/v1/session",
            headers={"Origin": settings.public_origin},
            json={"username": "member", "password": "secret"},
        )
        member_csrf = member_login.json()["csrf_token"]
        # 最后以 owner 登录，使默认会话为 owner（其他测试依赖 owner 身份）
        owner_login = client.post(
            "/api/v1/session",
            headers={"Origin": settings.public_origin},
            json={"username": "owner", "password": "secret"},
        )
        yield {
            "client": client,
            "csrf": {"X-CSRF-Token": owner_login.json()["csrf_token"]},
            "member_csrf": member_csrf,
            "cymail_id": cymail_id,
            "other_id": other_id,
            "origin": settings.public_origin,
        }


def test_list_aliases_reads_with_member_and_hides_secret(cymail_app):
    client = cymail_app["client"]
    response = client.get(
        "/api/v1/sources/cymail/aliases",
        params={"connection_id": cymail_app["cymail_id"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body[0]["provider_key"] == ALIAS_ID
    assert body[0]["address"] == "user@icloud.com"
    assert SECRET not in response.text
    requested = [r for r in UPSTREAM["requests"] if r.url.path == "/api/aliases"]
    assert requested and requested[0].headers["X-API-Key"] == SECRET


def test_create_alias_requires_csrf_and_sends_label(cymail_app):
    client = cymail_app["client"]
    params = {"connection_id": cymail_app["cymail_id"]}
    no_csrf = client.post(
        "/api/v1/sources/cymail/aliases", params=params, json={"label": "x"}
    )
    assert (no_csrf.status_code, no_csrf.json()["detail"]) == (403, "CSRF_TOKEN_INVALID")

    created = client.post(
        "/api/v1/sources/cymail/aliases",
        params=params,
        headers=cymail_app["csrf"],
        json={"label": "GitHub"},
    )
    assert created.status_code == 201
    assert created.json()["provider_key"] == "fresh@icloud.com"
    assert created.json()["detail"]["interface"] == "icloud_web"
    assert SECRET not in created.text
    post = [r for r in UPSTREAM["requests"] if r.method == "POST" and r.url.path == "/api/aliases"]
    assert post and json.loads(post[0].content) == {"account_id": ACCOUNT, "label": "GitHub"}


def test_delete_alias_returns_204(cymail_app):
    client = cymail_app["client"]
    response = client.request(
        "DELETE",
        f"/api/v1/sources/cymail/aliases/{ALIAS_ID}",
        params={"connection_id": cymail_app["cymail_id"]},
        headers=cymail_app["csrf"],
    )
    assert response.status_code == 204


def test_deactivate_alias_returns_success(cymail_app):
    client = cymail_app["client"]
    response = client.post(
        f"/api/v1/sources/cymail/aliases/{ALIAS_ID}/deactivate",
        params={"connection_id": cymail_app["cymail_id"]},
        headers=cymail_app["csrf"],
    )
    assert response.status_code == 200
    assert response.json() == {"provider_key": ALIAS_ID, "success": True}


def test_member_cannot_write(cymail_app):
    client = cymail_app["client"]
    member = client.post(
        "/api/v1/session",
        headers={"Origin": cymail_app["origin"]},
        json={"username": "member", "password": "secret"},
    )
    assert member.json()["user"]["role"] == "member"
    response = client.post(
        "/api/v1/sources/cymail/aliases",
        params={"connection_id": cymail_app["cymail_id"]},
        headers={"X-CSRF-Token": member.json()["csrf_token"]},
        json={"label": "x"},
    )
    assert (response.status_code, response.json()["detail"]) == (403, "OWNER_REQUIRED")


def test_upstream_errors_map_to_http_status(cymail_app):
    client = cymail_app["client"]
    params = {"connection_id": cymail_app["cymail_id"]}

    UPSTREAM["mode"] = "auth"
    auth = client.get("/api/v1/sources/cymail/aliases", params=params)
    assert (auth.status_code, auth.json()["detail"]) == (502, "SOURCE_AUTH")

    UPSTREAM["mode"] = "conflict"
    conflict = client.post(
        "/api/v1/sources/cymail/aliases",
        params=params,
        headers=cymail_app["csrf"],
        json={"label": "x"},
    )
    assert (conflict.status_code, conflict.json()["detail"]) == (409, "SOURCE_CONFLICT")

    UPSTREAM["mode"] = "timeout"
    timeout = client.get("/api/v1/sources/cymail/aliases", params=params)
    assert (timeout.status_code, timeout.json()["detail"]) == (504, "SOURCE_TIMEOUT")

    UPSTREAM["mode"] = "ok"


def test_non_cymail_connection_is_not_found(cymail_app):
    client = cymail_app["client"]
    response = client.get(
        "/api/v1/sources/cymail/aliases",
        params={"connection_id": cymail_app["other_id"]},
    )
    assert (response.status_code, response.json()["detail"]) == (404, "CONNECTION_NOT_FOUND")


def test_inventory_sync_requires_owner(cymail_app):
    client = cymail_app["client"]
    response = client.post(
        "/api/v1/sources/cymail/inventory/sync",
        params={"connection_id": cymail_app["cymail_id"]},
        headers=cymail_app["csrf"],
    )
    assert response.status_code == 200
    assert response.json() == {"account_id": ACCOUNT, "synced": 3}


def test_requires_authentication(cymail_app):
    client = cymail_app["client"]
    client.cookies.clear()
    response = client.get(
        "/api/v1/sources/cymail/aliases",
        params={"connection_id": cymail_app["cymail_id"]},
    )
    assert response.status_code == 401
