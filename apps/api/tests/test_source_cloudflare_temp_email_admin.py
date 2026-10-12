"""Cloudflare 临时邮箱来源管理：适配器契约测试 + 路由集成测试。

上游契约以 Worker 源码 admin_api/ 为准：
- 认证头 x-admin-auth；管理列表 limit/offset 必填且 1..100；count 仅在 offset=0 返回。
- /admin/new_address 必须带 JSON body 且 name 必填。
- reset_password 的 password 字段是明文口令的 SHA-256 十六进制。
"""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from mails_api.app import create_app
from mails_api.models import Base, ProviderConnection, User
from mails_api.security.passwords import hash_password
from mails_api.settings import Settings
from mails_api.sources_admin import admin_for
from mails_api.sources_admin.base import SOURCE_ERROR_CODES, SourceAdminError
from mails_api.sources_admin.cloudflare_temp_email import CloudflareTempEmailAdmin

BASE_URL = "https://cf-mail.example"
SECRET = "admin-secret-value"

KEY = bytes(range(32))


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _address_row(**overrides) -> dict:
    return {
        "id": 125,
        "name": "tmp809a7130cd07@orangeai.cfd",
        "source_meta": "admin",
        "created_at": "2026-10-11 12:47:08",
        "updated_at": "2026-10-11 12:47:08",
        "mail_count": 3,
        "send_count": 0,
        **overrides,
    }


def _address_page(rows, count=None) -> httpx.Response:
    return httpx.Response(200, json={"results": rows, "count": len(rows) if count is None else count})


# --------------------------------------------------------------------------- adapter


@pytest.mark.asyncio
async def test_list_items_maps_address_rows_and_uses_admin_auth():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == "/admin/address"
        assert request.headers["x-admin-auth"] == SECRET
        assert request.url.params["limit"] == "100"
        assert request.url.params["offset"] == "0"
        return _address_page([_address_row()], count=1)

    async with _client(handler) as client:
        items = await CloudflareTempEmailAdmin(client).list_items(BASE_URL, SECRET)

    assert len(calls) == 1
    assert len(items) == 1
    item = items[0]
    assert item.provider_key == "125"
    assert item.address == "tmp809a7130cd07@orangeai.cfd"
    assert item.status == "active"
    assert item.created_at == "2026-10-11T12:47:08Z"
    assert item.detail == {"source_meta": "admin", "mail_count": 3, "send_count": 0}
    assert item.payload()["provider_key"] == "125"


@pytest.mark.asyncio
async def test_list_items_paginates_until_short_page():
    offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        if offset == 0:
            return _address_page([_address_row(id=i, name=f"a{i}@x.cfd") for i in range(100)], count=101)
        return _address_page([_address_row(id=999, name="last@x.cfd")], count=0)

    async with _client(handler) as client:
        items = await CloudflareTempEmailAdmin(client).list_items(BASE_URL, SECRET)

    assert offsets == [0, 100]
    assert len(items) == 101
    assert items[-1].provider_key == "999"


@pytest.mark.asyncio
async def test_create_item_posts_required_json_body_without_leaking_credentials():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/admin/new_address"
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "jwt": "super-secret-jwt",
                "address": "tmpabc@orangeai.cfd",
                "password": "plain-generated-password",
                "address_id": 555,
            },
        )

    async with _client(handler) as client:
        item = await CloudflareTempEmailAdmin(client).create_item(
            BASE_URL, SECRET, {"name": "tmpabc", "enable_prefix": True}
        )

    body = httpx.Response(200, content=captured["body"]).json()
    assert body == {"name": "tmpabc", "enablePrefix": True}
    assert item.provider_key == "555"
    assert item.address == "tmpabc@orangeai.cfd"
    assert item.status == "active"
    assert item.detail == {}
    assert "jwt" not in item.payload()
    assert "super-secret-jwt" not in str(item.payload())
    assert "plain-generated-password" not in str(item.payload())


@pytest.mark.asyncio
async def test_create_item_requires_name():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("upstream must not be called without a name")

    async with _client(handler) as client:
        with pytest.raises(SourceAdminError) as raised:
            await CloudflareTempEmailAdmin(client).create_item(BASE_URL, SECRET, {"name": "  "})
    assert raised.value.code == "SOURCE_REJECTED"


@pytest.mark.asyncio
async def test_delete_clear_mails_and_password_actions_use_expected_contract():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/reset_password"):
            seen.append(("body", httpx.Response(200, content=request.content).json()))
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/admin/show_password/125":
            return httpx.Response(200, json={"jwt": "signed-token-value"})
        if request.url.path == "/admin/mails":
            assert request.url.params["address"] == "tmp@x.cfd"
            assert request.url.params["limit"] == "10"
            assert request.url.params["offset"] == "0"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 9,
                            "address": "tmp@x.cfd",
                            "source": "smtp",
                            "message_id": "m-1",
                            "created_at": "2026-10-11 12:47:08",
                            "is_unread": 1,
                            "raw": "From: secret-body",
                        }
                    ],
                    "count": 1,
                },
            )
        return httpx.Response(200, json={"success": True})

    async with _client(handler) as client:
        adapter = CloudflareTempEmailAdmin(client)
        await adapter.delete_item(BASE_URL, SECRET, "125")
        await adapter.clear_inbox(BASE_URL, SECRET, "125")
        mails = await adapter.list_mails(BASE_URL, SECRET, address="tmp@x.cfd", limit=10, offset=0)
        await adapter.delete_mail(BASE_URL, SECRET, "9")
        password = await adapter.show_password(BASE_URL, SECRET, "125")
        await adapter.reset_password(BASE_URL, SECRET, "125", "new-plain-password")

    assert ("DELETE", "/admin/delete_address/125") in seen
    assert ("DELETE", "/admin/clear_inbox/125") in seen
    assert ("DELETE", "/admin/mails/9") in seen
    assert ("POST", "/admin/address/125/reset_password") in seen
    assert mails == {
        "count": 1,
        "items": [
            {
                "provider_key": "9",
                "address": "tmp@x.cfd",
                "source": "smtp",
                "message_id": "m-1",
                "created_at": "2026-10-11T12:47:08Z",
                "is_unread": True,
            }
        ],
    }
    # raw 正文绝不进入管理视图
    assert "secret-body" not in str(mails)
    assert password == {
        "provider_key": "125",
        "sensitive": True,
        "field": "jwt",
        "value": "signed-token-value",
    }
    # 上游约定：reset_password 的 password 是明文口令的 SHA-256 十六进制，绝不发送明文
    body = next(value for kind, value in seen if kind == "body")
    assert body == {"password": hashlib.sha256(b"new-plain-password").hexdigest()}
    assert "new-plain-password" not in str(body)


@pytest.mark.asyncio
async def test_probe_hits_health_check_and_rejects_empty_body():
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health_check"
        return httpx.Response(200, text="OK")

    async with _client(ok) as client:
        await CloudflareTempEmailAdmin(client).probe(BASE_URL, SECRET)

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="")

    async with _client(empty) as client:
        with pytest.raises(SourceAdminError) as raised:
            await CloudflareTempEmailAdmin(client).probe(BASE_URL, SECRET)
    assert raised.value.code == "SOURCE_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code,retries",
    [
        (401, {"message": SECRET}, "SOURCE_AUTH", 1),
        (403, {"message": SECRET}, "SOURCE_AUTH", 1),
        (404, {"message": SECRET}, "SOURCE_NOT_FOUND", 1),
        (409, {"message": SECRET}, "SOURCE_CONFLICT", 1),
        (400, SECRET, "SOURCE_REJECTED", 1),
        (500, {"message": SECRET}, "SOURCE_UNAVAILABLE", 2),
        (503, {"message": SECRET}, "SOURCE_UNAVAILABLE", 2),
    ],
)
async def test_status_codes_map_to_fixed_error_codes(status, body, code, retries):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    async with _client(handler) as client:
        with pytest.raises(SourceAdminError) as raised:
            await CloudflareTempEmailAdmin(client).list_items(BASE_URL, SECRET)

    assert raised.value.code == code
    assert raised.value.code in SOURCE_ERROR_CODES
    assert SECRET not in str(raised.value)
    assert len(calls) == retries


@pytest.mark.asyncio
async def test_timeout_maps_to_source_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    async with _client(handler) as client:
        with pytest.raises(SourceAdminError) as raised:
            await CloudflareTempEmailAdmin(client).list_items(BASE_URL, SECRET)
    assert raised.value.code == "SOURCE_TIMEOUT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [1, 2, 3],
        {"results": {}, "count": 0},
        {"results": [], "count": "nope"},
        {"results": [{"name": "no-id@x.cfd", "created_at": "2026-10-11 12:47:08"}], "count": 1},
        {"results": [{"id": 1, "created_at": "2026-10-11 12:47:08"}], "count": 1},
        {"results": [{"id": 1, "name": "a@x.cfd", "created_at": "not-a-date"}], "count": 1},
    ],
)
async def test_contract_violations_raise_source_contract(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _client(handler) as client:
        with pytest.raises(SourceAdminError) as raised:
            await CloudflareTempEmailAdmin(client).list_items(BASE_URL, SECRET)
    assert raised.value.code == "SOURCE_CONTRACT"


@pytest.mark.asyncio
async def test_delete_requires_success_flag():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False})

    async with _client(handler) as client:
        with pytest.raises(SourceAdminError) as raised:
            await CloudflareTempEmailAdmin(client).delete_item(BASE_URL, SECRET, "1")
    assert raised.value.code == "SOURCE_CONTRACT"


def test_build_returns_admin_for_cloudflare_kind():
    import asyncio

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            adapter = admin_for("cloudflare_temp_email", client)
            assert isinstance(adapter, CloudflareTempEmailAdmin)

    asyncio.run(run())


# ----------------------------------------------------------------------------- routes


def _upstream(request: httpx.Request) -> httpx.Response:
    assert request.headers["x-admin-auth"] == "cf-secret"
    path = request.url.path
    if path == "/health_check":
        return httpx.Response(200, text="OK")
    if path == "/admin/address":
        return httpx.Response(200, json={"results": [_address_row()], "count": 1})
    if path == "/admin/new_address":
        return httpx.Response(
            200,
            json={
                "jwt": "jwt-should-not-leak",
                "address": "tmpnew@orangeai.cfd",
                "address_id": 777,
                "password": "pw-should-not-leak",
            },
        )
    if path == "/admin/mails":
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 9,
                        "address": "tmp809a7130cd07@orangeai.cfd",
                        "source": "smtp",
                        "created_at": "2026-10-11 12:47:08",
                        "is_unread": 1,
                        "raw": "raw-body-should-not-leak",
                    }
                ],
                "count": 1,
            },
        )
    if path == "/admin/show_password/125":
        return httpx.Response(200, json={"jwt": "address-credential-value"})
    return httpx.Response(200, json={"success": True})


def _settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        encryption_key=base64.b64encode(KEY).decode(),
        public_origin="http://localhost:5173",
        upstream_origins=["https://cf.example"],
    )


def _app_client(engine):
    settings = _settings()
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(_upstream))
    app = create_app(settings, engine, async_client)
    from mails_api.routes.source_cloudflare_temp_email import router as source_router

    app.include_router(source_router)
    return TestClient(app), settings


def _login(client: TestClient, settings: Settings) -> dict:
    response = client.post(
        "/api/v1/session",
        headers={"Origin": settings.public_origin},
        json={"username": "owner", "password": "secret"},
    )
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def _seed_owner(engine) -> None:
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(username="owner", password_hash=hash_password("secret")))
        db.commit()


def test_routes_require_csrf_and_manage_the_source(engine):
    _seed_owner(engine)
    client, settings = _app_client(engine)
    with client:
        csrf = _login(client, settings)
        created = client.post(
            "/api/v1/connections",
            headers=csrf,
            json={
                "kind": "cloudflare_temp_email",
                "name": "临时邮箱",
                "base_url": "https://cf.example",
                "api_key": "cf-secret",
            },
        )
        assert created.status_code == 201
        connection_id = created.json()["id"]
        base = f"/api/v1/sources/cloudflare_temp_email/{connection_id}"

        items = client.get(f"{base}/items")
        assert items.status_code == 200
        assert items.json()[0]["provider_key"] == "125"
        assert "cf-secret" not in items.text

        assert client.post(f"{base}/items", json={"name": "tmpnew"}).status_code == 403

        made = client.post(f"{base}/items", headers=csrf, json={"name": "tmpnew"})
        assert made.status_code == 201
        assert made.json()["provider_key"] == "777"
        assert "jwt-should-not-leak" not in made.text
        assert "pw-should-not-leak" not in made.text

        assert client.delete(f"{base}/items/125", headers=csrf).status_code == 204
        assert client.post(f"{base}/items/125/clear_inbox", headers=csrf).status_code == 204

        mails = client.get(f"{base}/mails", params={"address": "tmp@x.cfd", "limit": 10})
        assert mails.status_code == 200
        assert mails.json()["count"] == 1
        assert mails.json()["items"][0]["provider_key"] == "9"
        assert "raw-body-should-not-leak" not in mails.text

        assert client.delete(f"{base}/mails/9", headers=csrf).status_code == 204

        password = client.get(f"{base}/items/125/password")
        assert password.status_code == 200
        assert password.json() == {
            "provider_key": "125",
            "sensitive": True,
            "field": "jwt",
            "value": "address-credential-value",
        }

        reset = client.post(
            f"{base}/items/125/password", headers=csrf, json={"password": "brand-new-password"}
        )
        assert reset.status_code == 204

        assert client.post(f"{base}/probe", headers=csrf).json() == {"status": "ok"}


def test_route_rejects_unknown_kind(engine):
    _seed_owner(engine)
    with Session(engine) as db:
        connection = ProviderConnection(
            kind="cymail",
            name="CYMail",
            base_url="https://cf.example",
            encrypted_secret=b"opaque",
            config_json={},
            status="active",
        )
        db.add(connection)
        db.commit()
        connection_id = str(connection.id)

    client, settings = _app_client(engine)
    with client:
        _login(client, settings)
        response = client.get(f"/api/v1/sources/cloudflare_temp_email/{connection_id}/items")
    assert response.status_code == 422
    assert response.json()["detail"] == "UNSUPPORTED_PROVIDER_KIND"


def test_route_maps_missing_connection_and_upstream_errors(engine):
    _seed_owner(engine)

    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health_check":
            return httpx.Response(200, text="OK")
        return httpx.Response(401, json={"message": "nope"})

    settings = _settings()
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    app = create_app(settings, engine, async_client)
    from mails_api.routes.source_cloudflare_temp_email import router as source_router

    app.include_router(source_router)
    with TestClient(app) as client:
        csrf = _login(client, settings)
        created = client.post(
            "/api/v1/connections",
            headers=csrf,
            json={
                "kind": "cloudflare_temp_email",
                "name": "临时邮箱",
                "base_url": "https://cf.example",
                "api_key": "cf-secret",
            },
        )
        connection_id = created.json()["id"]

        missing = client.get("/api/v1/sources/cloudflare_temp_email/00000000-0000-0000-0000-000000000000/items")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "CONNECTION_NOT_FOUND"

        unauthorized = client.get(f"/api/v1/sources/cloudflare_temp_email/{connection_id}/items")
        assert unauthorized.status_code == 502
        assert unauthorized.json()["detail"] == "SOURCE_AUTH"
