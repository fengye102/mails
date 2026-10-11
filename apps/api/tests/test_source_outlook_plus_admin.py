"""OutlookEmailPlus（OEP）来源管理适配器的契约测试。

上游 mock 严格复刻 outlook-email-plus v2.9.10 容器内 ``/app/outlook_web`` 的真实契约：

- 鉴权走「登录会话」：``POST /login {"password": ...}`` → ``200 {"success": true}`` + ``Set-Cookie``；
  之后的 ``/api/accounts*`` 必须携带该 Cookie，否则 ``401 AUTH_REQUIRED``。
- 写操作（``POST``/``PUT``/``PATCH``/``DELETE``）还必须带会话级 ``X-CSRFToken``，取值来自
  ``GET /api/csrf-token`` → ``{"csrf_token": ...}``（注意该响应**没有** ``success`` 字段）；
  缺了会 ``400 CSRF_TOKEN_INVALID``。只读请求不需要。
- 列账号：``GET /api/accounts?page=&page_size=`` → ``{"success": true, "accounts": [...],
  "pagination": {...}}``。
- 增账号：``POST /api/accounts`` → ``{"success": true, "summary": {"imported": n, ...}}``；
  失败为 ``400`` + ``error.code``。
- 删账号：``DELETE /api/accounts/email/<email>`` → ``{"success": true}``。
- 令牌刷新：``POST /api/accounts/<id>/refresh``（成功/失败都可能是 ``200``）。
- 导出：``POST /api/export/verify`` → verify_token，再 ``GET /api/accounts/export`` + ``X-Export-Token``。

这些测试只走 MockTransport，不触碰任何真实 OEP 实例，也不做任何写操作。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from mails_api.app import create_app
from mails_api.models import Base, ProviderConnection, User
from mails_api.routes.source_outlook_plus import router as source_router
from mails_api.security.crypto import encrypt_secret
from mails_api.security.passwords import hash_password
from mails_api.settings import Settings
from mails_api.sources_admin import outlook_plus
from mails_api.sources_admin.base import SOURCE_ERROR_CODES, SourceAdminError
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

BASE_URL = "https://oep.example"
PASSWORD = "login-password"
COOKIE = "cookie-1"
CSRF_COOKIE = "cookie-2"
CSRF_TOKEN = "csrf-1"


def account(index: int, **overrides):
    return {
        "id": index,
        "email": f"user{index}@outlook.com",
        "account_type": "outlook",
        "provider": "outlook",
        "status": "active",
        "created_at": "2026-07-09 06:32:10",
        "last_refresh_at": "2026-07-09T06:32:10Z",
        "group_name": "默认分组",
        "remark": "",
        **overrides,
    }


def upstream(server):
    """按 server 状态机返回真实 OEP 形状的响应。"""

    def handler(request: httpx.Request) -> httpx.Response:
        server["requests"].append(request)
        path = request.url.path
        mode = server["mode"]
        if path == "/login":
            if mode == "login_timeout":
                raise httpx.ReadTimeout("slow", request=request)
            if mode == "login_error":
                raise httpx.ConnectError("down", request=request)
            if mode == "login_500":
                return httpx.Response(500, text="boom")
            if mode == "login_429":
                return httpx.Response(
                    429,
                    json={"success": False, "error": {"code": "LOGIN_RATE_LIMITED"}},
                )
            body = json.loads(request.content or b"{}")
            if body.get("password") != PASSWORD:
                return httpx.Response(
                    401,
                    json={
                        "success": False,
                        "error": {"code": "LOGIN_INVALID_PASSWORD"},
                    },
                )
            return httpx.Response(
                200,
                json={"success": True, "message": "登录成功"},
                headers={"Set-Cookie": f"session={COOKIE}; Path=/; HttpOnly"},
            )
        # 以下都需要会话 Cookie（CSRF 端点会轮换会话 Cookie，两个值都算有效）。
        cookie_header = request.headers.get("cookie", "")
        if f"session={COOKIE}" not in cookie_header and f"session={CSRF_COOKIE}" not in cookie_header:
            return httpx.Response(
                401,
                json={"success": False, "code": "AUTH_REQUIRED", "need_login": True},
            )
        # 写操作必须带会话级 CSRF token（真实 OEP 缺了会 400 CSRF_TOKEN_INVALID）。
        if path == "/api/csrf-token" and request.method == "GET":
            # 真实 OEP 会把 token 写进服务端会话并下发新的会话 Cookie；客户端丢掉它就会
            # 在下一次写操作报 "The CSRF session token is missing."。
            return httpx.Response(
                200,
                json={"csrf_token": CSRF_TOKEN},
                headers={"Set-Cookie": f"session={CSRF_COOKIE}; Path=/; HttpOnly"},
            )
        is_write = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        if is_write:
            if request.headers.get("x-csrftoken") != CSRF_TOKEN:
                return httpx.Response(
                    400,
                    json={
                        "success": False,
                        "error": {"code": "CSRF_TOKEN_INVALID", "message": "CSRF token missing"},
                    },
                )
            if f"session={CSRF_COOKIE}" not in cookie_header:
                return httpx.Response(
                    400,
                    json={
                        "success": False,
                        "error": {
                            "code": "CSRF_TOKEN_INVALID",
                            "details": "The CSRF session token is missing.",
                        },
                    },
                )
        if mode == "accounts_500":
            return httpx.Response(503, text="overloaded")
        if mode == "accounts_nonjson":
            return httpx.Response(200, text="not json")
        if mode == "accounts_badshape":
            return httpx.Response(200, json={"success": True, "accounts": "nope"})
        if path == "/api/accounts" and request.method == "GET":
            page = int(request.url.params.get("page", "1"))
            # 真实 OEP：page_size 上限 100；页码越界会被夹回最后一页（不是返回空页）。
            page_size = min(int(request.url.params.get("page_size", "50")), 100)
            if server["pages"] is not None:
                source = [item for chunk in server["pages"].values() for item in chunk]
            else:
                source = server["accounts"]
            total_count = len(source)
            total_pages = (total_count + page_size - 1) // page_size if total_count else 0
            effective_page = min(page, total_pages) if total_pages else 1
            start = (effective_page - 1) * page_size
            chunk = source[start : start + page_size]
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "accounts": chunk,
                    "pagination": {
                        "page": effective_page,
                        "page_size": page_size,
                        "total_count": total_count,
                        "total_pages": total_pages,
                    },
                },
            )
        if path == "/api/accounts" and request.method == "POST":
            if mode == "create_400":
                return httpx.Response(
                    400,
                    json={
                        "success": False,
                        "error": {"code": "ACCOUNT_IMPORT_FAILED"},
                        "message": PASSWORD,
                    },
                )
            if mode == "create_zero":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "message": "导入完成：成功 0 个，失败 1 个",
                        "summary": {"imported": 0, "failed": 1},
                        "errors": [{"line": 1, "error": PASSWORD}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "message": "导入完成：成功 1 个，失败 0 个",
                    "summary": {"imported": 1, "failed": 0},
                    "errors": [],
                },
            )
        if path.startswith("/api/accounts/email/") and request.method == "DELETE":
            if mode == "delete_404":
                return httpx.Response(404, json={"success": False})
            if mode == "delete_409":
                return httpx.Response(409, json={"success": False})
            return httpx.Response(200, json={"success": True})
        if path.endswith("/refresh") and request.method == "POST":
            if mode == "refresh_notfound":
                return httpx.Response(
                    200, json={"success": False, "error": {"code": "ACCOUNT_NOT_FOUND"}}
                )
            if mode == "refresh_failed":
                return httpx.Response(
                    200, json={"success": False, "error": {"code": "TOKEN_REFRESH_FAILED"}}
                )
            return httpx.Response(200, json={"success": True, "message": "Token 刷新成功"})
        if path == "/api/export/verify":
            if mode == "export_401":
                return httpx.Response(401, json={"success": False})
            return httpx.Response(200, json={"success": True, "verify_token": "verify-1"})
        if path == "/api/accounts/export":
            if request.headers.get("x-export-token") != "verify-1":
                return httpx.Response(403, json={"success": False})
            return httpx.Response(
                200,
                text="user1@outlook.com----pw----cid----rt\n",
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
        raise AssertionError(f"unexpected upstream: {request.method} {path}")

    return handler


@pytest.fixture
def server():
    return {
        "accounts": [account(1), account(2)],
        "pages": None,
        "mode": "ok",
        "requests": [],
    }


def client_for(server) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(upstream(server)))


def test_build_returns_adapter_with_protocol_methods():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"success": True}))
    adapter = outlook_plus.build(httpx.AsyncClient(transport=transport))
    for name in ("probe", "list_items", "create_item", "delete_item"):
        assert callable(getattr(adapter, name))


@pytest.mark.asyncio
async def test_probe_logs_in_then_checks_accounts_contract(server):
    async with client_for(server) as client:
        assert await outlook_plus.build(client).probe(BASE_URL, PASSWORD) is None
    paths = [r.url.path for r in server["requests"]]
    assert paths == ["/login", "/api/accounts"]
    login_body = json.loads(server["requests"][0].content)
    assert login_body == {"password": PASSWORD}
    # 第二次请求必须带会话 Cookie。
    assert f"session={COOKIE}" in server["requests"][1].headers["cookie"]


@pytest.mark.asyncio
async def test_probe_wrong_password_is_source_auth(server):
    async with client_for(server) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).probe(BASE_URL, "wrong")
    assert exc.value.code == "SOURCE_AUTH"
    assert "wrong" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,code",
    [
        ("login_timeout", "SOURCE_TIMEOUT"),
        ("login_error", "SOURCE_UNAVAILABLE"),
        ("login_500", "SOURCE_UNAVAILABLE"),
        ("login_429", "SOURCE_REJECTED"),
        ("accounts_500", "SOURCE_UNAVAILABLE"),
        ("accounts_nonjson", "SOURCE_CONTRACT"),
        ("accounts_badshape", "SOURCE_CONTRACT"),
    ],
)
async def test_transport_failures_map_to_fixed_codes(server, mode, code):
    server["mode"] = mode
    async with client_for(server) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).probe(BASE_URL, PASSWORD)
    assert exc.value.code == code
    assert exc.value.code in SOURCE_ERROR_CODES
    assert PASSWORD not in str(exc.value)


@pytest.mark.asyncio
async def test_probe_rejects_expired_session_401(server):
    # /api/accounts 返回 401 AUTH_REQUIRED（会话失效）。
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"success": True}, headers={"Set-Cookie": "session=x"})
        return httpx.Response(401, json={"success": False, "code": "AUTH_REQUIRED"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).probe(BASE_URL, PASSWORD)
    assert exc.value.code == "SOURCE_AUTH"


@pytest.mark.asyncio
async def test_list_items_maps_account_fields(server):
    async with client_for(server) as client:
        items = await outlook_plus.build(client).list_items(BASE_URL, PASSWORD)
    assert [i.provider_key for i in items] == ["user1@outlook.com", "user2@outlook.com"]
    first = items[0]
    assert first.address == "user1@outlook.com"
    assert first.status == "active"
    assert first.created_at == "2026-07-09T06:32:10Z"
    assert first.detail["id"] == 1
    assert first.detail["account_type"] == "outlook"
    assert first.detail["group_name"] == "默认分组"
    assert first.detail["last_refresh_at"] == "2026-07-09T06:32:10Z"
    assert PASSWORD not in json.dumps(first.payload())


@pytest.mark.asyncio
async def test_list_items_paginates_until_short_page(server):
    server["pages"] = {1: [account(i) for i in range(1, 101)], 2: [account(101)]}
    async with client_for(server) as client:
        items = await outlook_plus.build(client).list_items(BASE_URL, PASSWORD)
    assert len(items) == 101
    pages = [
        r.url.params["page"] for r in server["requests"] if r.url.path == "/api/accounts"
    ]
    assert pages == ["1", "2"]


@pytest.mark.asyncio
async def test_list_items_stops_on_total_count_when_page_is_full(server):
    """满页必须靠 pagination.total_count 收尾。

    真实 OEP 会把越界页码夹回最后一页，所以「返回不足一页就结束」的写法会无限翻页，
    最终撞上 _MAX_PAGES 报 SOURCE_CONTRACT（线上实测：110 秒后才失败）。
    """
    server["accounts"] = [account(i) for i in range(1, 101)]  # 正好一页
    async with client_for(server) as client:
        items = await outlook_plus.build(client).list_items(BASE_URL, PASSWORD)
    assert len(items) == 100
    pages = [r.url.params["page"] for r in server["requests"] if r.url.path == "/api/accounts"]
    assert pages == ["1"]  # 不允许多翻一页


@pytest.mark.asyncio
async def test_list_items_handles_multiple_full_pages(server):
    server["accounts"] = [account(i) for i in range(1, 251)]
    async with client_for(server) as client:
        items = await outlook_plus.build(client).list_items(BASE_URL, PASSWORD)
    assert len(items) == 250
    assert len({i.provider_key for i in items}) == 250
    pages = [r.url.params["page"] for r in server["requests"] if r.url.path == "/api/accounts"]
    assert pages == ["1", "2", "3"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        {"id": 1},  # 缺 email
        {"id": 1, "email": ""},  # email 为空
        {"id": 1, "email": 5},  # email 类型不对
        {"id": 1, "email": "user@x.com", "created_at": "not-a-date"},
        {"id": 1, "email": "user@x.com", "status": ["active"]},
    ],
)
async def test_list_items_malformed_account_is_contract(server, item):
    server["accounts"] = [item]
    async with client_for(server) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).list_items(BASE_URL, PASSWORD)
    assert exc.value.code == "SOURCE_CONTRACT"


@pytest.mark.asyncio
async def test_create_item_sends_account_string(server):
    async with client_for(server) as client:
        item = await outlook_plus.build(client).create_item(
            BASE_URL,
            PASSWORD,
            {
                "account_string": "new@outlook.com----pw----client----token",
                "group_id": 2,
            },
        )
    assert item.provider_key == "new@outlook.com"
    assert item.address == "new@outlook.com"
    assert item.status == "active"
    post = next(r for r in server["requests"] if r.method == "POST" and r.url.path == "/api/accounts")
    body = json.loads(post.content)
    assert body["account_string"] == "new@outlook.com----pw----client----token"
    assert body["group_id"] == 2
    assert body["provider"] == "outlook"
    assert "pw" not in json.dumps(item.payload())


@pytest.mark.asyncio
async def test_create_item_from_fields_builds_account_string(server):
    async with client_for(server) as client:
        await outlook_plus.build(client).create_item(
            BASE_URL,
            PASSWORD,
            {
                "email": "x@outlook.com",
                "password": "pw",
                "client_id": "cid",
                "refresh_token": "rt",
            },
        )
    post = next(r for r in server["requests"] if r.method == "POST" and r.url.path == "/api/accounts")
    assert json.loads(post.content)["account_string"] == "x@outlook.com----pw----cid----rt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,code",
    [
        ("create_400", "SOURCE_REJECTED"),
        ("create_zero", "SOURCE_REJECTED"),
    ],
)
async def test_create_item_rejections(server, mode, code):
    server["mode"] = mode
    async with client_for(server) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).create_item(
                BASE_URL, PASSWORD, {"account_string": "a@b.com----p----c----t"}
            )
    assert exc.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"account_string": ""},
        {"account_string": "no-at-sign"},
        {"account_string": "a@b.com----p----c----t", "group_id": "2"},
        {"account_string": "a@b.com----p----c----t", "add_to_pool": "yes"},
        {"email": "a@b.com", "password": 5},
        "not-a-dict",
    ],
)
async def test_create_item_bad_input_is_contract(server, payload):
    async with client_for(server) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).create_item(BASE_URL, PASSWORD, payload)
    assert exc.value.code == "SOURCE_CONTRACT"
    assert not [
        r for r in server["requests"] if r.method == "POST" and r.url.path == "/api/accounts"
    ]


@pytest.mark.asyncio
async def test_delete_item_uses_email_endpoint(server):
    async with client_for(server) as client:
        assert (
            await outlook_plus.build(client).delete_item(BASE_URL, PASSWORD, "user1@outlook.com")
            is None
        )
    call = next(r for r in server["requests"] if r.method == "DELETE")
    assert call.url.path == "/api/accounts/email/user1@outlook.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,code",
    [
        ("delete_404", "SOURCE_NOT_FOUND"),
        ("delete_409", "SOURCE_CONFLICT"),
        ("accounts_500", "SOURCE_UNAVAILABLE"),
    ],
)
async def test_delete_item_error_mapping(server, mode, code):
    server["mode"] = mode
    async with client_for(server) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).delete_item(BASE_URL, PASSWORD, "user1@outlook.com")
    assert exc.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "a/b@x.com", "a b@x.com", "a@x.com\x00", "x@y.com\\z"])
async def test_delete_item_rejects_unsafe_key(server, key):
    async with client_for(server) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).delete_item(BASE_URL, PASSWORD, key)
    assert exc.value.code == "SOURCE_CONTRACT"
    assert not [r for r in server["requests"] if r.method == "DELETE"]


@pytest.mark.asyncio
async def test_refresh_token_success(server):
    async with client_for(server) as client:
        result = await outlook_plus.build(client).refresh_token(BASE_URL, PASSWORD, 7)
    assert result == {"account_id": 7, "status": "refreshed"}
    call = next(r for r in server["requests"] if r.url.path.endswith("/refresh"))
    assert call.url.path == "/api/accounts/7/refresh"
    assert call.method == "POST"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,code",
    [
        ("refresh_notfound", "SOURCE_NOT_FOUND"),
        ("refresh_failed", "SOURCE_REJECTED"),
    ],
)
async def test_refresh_token_error_mapping(server, mode, code):
    server["mode"] = mode
    async with client_for(server) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).refresh_token(BASE_URL, PASSWORD, 7)
    assert exc.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id", [0, -1, "7", True, None])
async def test_refresh_token_rejects_bad_id(server, account_id):
    async with client_for(server) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).refresh_token(BASE_URL, PASSWORD, account_id)
    assert exc.value.code == "SOURCE_CONTRACT"


@pytest.mark.asyncio
async def test_export_accounts_two_step_flow(server):
    async with client_for(server) as client:
        text = await outlook_plus.build(client).export_accounts(BASE_URL, PASSWORD)
    assert text == "user1@outlook.com----pw----cid----rt\n"
    paths = [r.url.path for r in server["requests"]]
    assert paths == ["/login", "/api/csrf-token", "/api/export/verify", "/api/accounts/export"]
    export_call = server["requests"][-1]
    assert export_call.headers["x-export-token"] == "verify-1"


@pytest.mark.asyncio
async def test_export_wrong_password_is_auth(server):
    server["mode"] = "export_401"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"success": True}, headers={"Set-Cookie": "session=x"})
        return httpx.Response(401, json={"success": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).export_accounts(BASE_URL, PASSWORD)
    assert exc.value.code == "SOURCE_AUTH"


@pytest.mark.asyncio
async def test_error_codes_stay_within_frozen_set(server):
    for mode, method in [
        ("accounts_500", "probe"),
        ("accounts_badshape", "probe"),
        ("create_400", "create"),
        ("delete_404", "delete"),
        ("refresh_failed", "refresh"),
    ]:
        server["mode"] = mode
        server["requests"].clear()
        async with client_for(server) as client:
            adapter = outlook_plus.build(client)
            with pytest.raises(SourceAdminError) as exc:
                if method == "probe":
                    await adapter.probe(BASE_URL, PASSWORD)
                elif method == "create":
                    await adapter.create_item(
                        BASE_URL, PASSWORD, {"account_string": "a@b.com----p----c----t"}
                    )
                elif method == "delete":
                    await adapter.delete_item(BASE_URL, PASSWORD, "a@b.com")
                else:
                    await adapter.refresh_token(BASE_URL, PASSWORD, 1)
        assert exc.value.code in SOURCE_ERROR_CODES


# ---------------------------------------------------------------------------
# 路由层：/api/v1/sources/outlook_plus/{connection_id}/...
# ---------------------------------------------------------------------------

KEY = bytes(range(32))


@pytest.fixture
def admin_api(engine, server):
    Base.metadata.create_all(engine)
    with DbSession(engine) as db:
        db.add(User(username="owner", password_hash=hash_password("secret")))
        connection = ProviderConnection(
            kind="outlook_plus",
            name="OEP",
            base_url="https://oep.example",
            encrypted_secret=encrypt_secret(KEY, PASSWORD),
            config_json={},
            status="active",
        )
        db.add(connection)
        db.commit()
        connection_id = str(connection.id)
    settings = Settings(
        database_url="sqlite://",
        encryption_key=base64.b64encode(KEY).decode(),
        public_origin="http://localhost:5173",
        upstream_origins=["https://oep.example"],
    )
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream(server)))
    # 生产接线在 app.py 里 include_router(source_router)；测试内显式挂载以独立验证本路由文件。
    app = create_app(settings, engine, async_client)
    app.include_router(source_router)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/session",
            headers={"Origin": settings.public_origin},
            json={"username": "owner", "password": "secret"},
        )
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
        yield client, csrf, connection_id, server


def test_route_lists_items_and_hides_secret(admin_api):
    client, _csrf, connection_id, _ = admin_api
    response = client.get(f"/api/v1/sources/outlook_plus/{connection_id}/items")
    assert response.status_code == 200
    assert [item["address"] for item in response.json()] == [
        "user1@outlook.com",
        "user2@outlook.com",
    ]
    assert PASSWORD not in response.text


def test_route_probe_reports_ok(admin_api):
    client, csrf, connection_id, _ = admin_api
    response = client.post(
        f"/api/v1/sources/outlook_plus/{connection_id}/probe", headers=csrf
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_route_creates_item(admin_api):
    client, csrf, connection_id, _ = admin_api
    response = client.post(
        f"/api/v1/sources/outlook_plus/{connection_id}/items",
        headers=csrf,
        json={"account_string": "new@outlook.com----p----c----t"},
    )
    assert response.status_code == 201
    assert response.json()["address"] == "new@outlook.com"
    assert PASSWORD not in response.text


def test_route_deletes_item(admin_api):
    client, csrf, connection_id, server = admin_api
    response = client.delete(
        f"/api/v1/sources/outlook_plus/{connection_id}/items/user1@outlook.com",
        headers=csrf,
    )
    assert response.status_code == 204
    assert any(
        request.method == "DELETE"
        and request.url.path == "/api/accounts/email/user1@outlook.com"
        for request in server["requests"]
    )


def test_route_refreshes_token(admin_api):
    client, csrf, connection_id, _ = admin_api
    response = client.post(
        f"/api/v1/sources/outlook_plus/{connection_id}/accounts/7/refresh",
        headers=csrf,
    )
    assert response.status_code == 200
    assert response.json() == {"account_id": 7, "status": "refreshed"}


def test_route_exports_accounts(admin_api):
    client, _csrf, connection_id, _ = admin_api
    response = client.get(f"/api/v1/sources/outlook_plus/{connection_id}/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "user1@outlook.com----pw----cid----rt\n"


def test_route_maps_upstream_error(admin_api):
    client, _csrf, connection_id, server = admin_api
    server["mode"] = "accounts_500"
    response = client.get(f"/api/v1/sources/outlook_plus/{connection_id}/items")
    assert response.status_code == 502
    assert response.json()["detail"] == "SOURCE_UNAVAILABLE"
    assert PASSWORD not in response.text


def test_route_requires_session(admin_api):
    client, csrf, connection_id, _ = admin_api
    client.cookies.clear()
    assert (
        client.get(f"/api/v1/sources/outlook_plus/{connection_id}/items").status_code
        == 401
    )
    assert (
        client.post(
            f"/api/v1/sources/outlook_plus/{connection_id}/items",
            headers=csrf,
            json={},
        ).status_code
        == 401
    )


def test_route_writes_require_csrf(admin_api):
    client, _csrf, connection_id, _ = admin_api
    assert (
        client.post(
            f"/api/v1/sources/outlook_plus/{connection_id}/items",
            json={"account_string": "a@b.com----p----c----t"},
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/v1/sources/outlook_plus/{connection_id}/items/user1@outlook.com"
        ).status_code
        == 403
    )


def test_route_member_cannot_write_or_export(admin_api, engine):
    client, csrf, connection_id, _ = admin_api
    with DbSession(engine) as db:
        user = db.scalar(select(User))
        user.role = "member"
        db.commit()
    assert (
        client.post(
            f"/api/v1/sources/outlook_plus/{connection_id}/items",
            headers=csrf,
            json={"account_string": "a@b.com----p----c----t"},
        ).status_code
        == 403
    )
    assert (
        client.get(f"/api/v1/sources/outlook_plus/{connection_id}/export").status_code
        == 403
    )
    # member 仍可读取列表。
    assert (
        client.get(f"/api/v1/sources/outlook_plus/{connection_id}/items").status_code
        == 200
    )


def test_route_missing_connection_is_404(admin_api):
    client, _csrf, _connection_id, _ = admin_api
    missing = "00000000-0000-0000-0000-000000000000"
    response = client.get(f"/api/v1/sources/outlook_plus/{missing}/items")
    assert response.status_code == 404
    assert response.json()["detail"] == "CONNECTION_NOT_FOUND"


def test_route_rejects_non_outlook_connection(admin_api, engine):
    client, _csrf, _connection_id, _ = admin_api
    with DbSession(engine) as db:
        other = ProviderConnection(
            kind="cymail",
            name="cy",
            base_url="https://oep.example",
            encrypted_secret=encrypt_secret(KEY, PASSWORD),
            config_json={},
            status="active",
        )
        db.add(other)
        db.commit()
        other_id = str(other.id)
    response = client.get(f"/api/v1/sources/outlook_plus/{other_id}/items")
    assert response.status_code == 422
    assert response.json()["detail"] == "UNSUPPORTED_PROVIDER_KIND"


# ==================== 会话级 CSRF（真实 OEP 写操作要求）====================


@pytest.mark.asyncio
async def test_create_item_fetches_session_csrf_and_sends_it(server):
    """写操作必须先取会话 CSRF token 再带上；真实 OEP 缺了会 400 CSRF_TOKEN_INVALID。"""
    async with client_for(server) as client:
        await outlook_plus.build(client).create_item(
            BASE_URL, PASSWORD, {"email": "user9@outlook.com", "client_id": "cid"}
        )
    paths = [r.url.path for r in server["requests"]]
    assert paths.count("/api/csrf-token") == 1
    writes = [r for r in server["requests"] if r.method == "POST" and r.url.path == "/api/accounts"]
    assert len(writes) == 1
    assert writes[0].headers["x-csrftoken"] == CSRF_TOKEN
    # CSRF 请求本身要带会话 Cookie。
    csrf_call = next(r for r in server["requests"] if r.url.path == "/api/csrf-token")
    assert f"session={COOKIE}" in csrf_call.headers["cookie"]


@pytest.mark.asyncio
async def test_delete_and_refresh_send_session_csrf(server):
    async with client_for(server) as client:
        adapter = outlook_plus.build(client)
        await adapter.delete_item(BASE_URL, PASSWORD, "user1@outlook.com")
        await adapter.refresh_token(BASE_URL, PASSWORD, 7)
    writes = [
        r
        for r in server["requests"]
        if r.url.path.startswith("/api/") and r.method in {"DELETE", "POST"}
    ]
    assert writes, "应当有写请求"
    assert all(r.headers.get("x-csrftoken") == CSRF_TOKEN for r in writes)
    # 每次写操作各自取一次 token（适配器无状态）。
    assert [r.url.path for r in server["requests"]].count("/api/csrf-token") == 2


@pytest.mark.asyncio
async def test_export_verify_sends_session_csrf(server):
    async with client_for(server) as client:
        text = await outlook_plus.build(client).export_accounts(BASE_URL, PASSWORD)
    assert "user1@outlook.com" in text
    verify_calls = [r for r in server["requests"] if r.url.path == "/api/export/verify"]
    assert len(verify_calls) == 1
    assert verify_calls[0].headers["x-csrftoken"] == CSRF_TOKEN


@pytest.mark.asyncio
async def test_read_only_calls_do_not_fetch_csrf(server):
    """只读链路不应多打一次 /api/csrf-token。"""
    async with client_for(server) as client:
        adapter = outlook_plus.build(client)
        await adapter.probe(BASE_URL, PASSWORD)
        await adapter.list_items(BASE_URL, PASSWORD)
    assert [r.url.path for r in server["requests"]].count("/api/csrf-token") == 0


@pytest.mark.asyncio
async def test_write_carries_session_cookie_rotated_by_csrf_endpoint(server):
    """CSRF 端点下发的新会话 Cookie 必须带到写请求上。

    真实 OEP：``GET /api/csrf-token`` 会把 token 写进服务端会话并 ``Set-Cookie`` 轮换会话；
    只带登录时那份旧 Cookie 的写请求会 400 ``CSRF_TOKEN_INVALID``（The CSRF session token
    is missing.）——线上实测踩到过。
    """
    async with client_for(server) as client:
        await outlook_plus.build(client).delete_item(BASE_URL, PASSWORD, "user1@outlook.com")
    delete_call = next(r for r in server["requests"] if r.method == "DELETE")
    assert f"session={CSRF_COOKIE}" in delete_call.headers["cookie"]
    assert delete_call.headers["x-csrftoken"] == CSRF_TOKEN


@pytest.mark.asyncio
async def test_csrf_endpoint_without_token_is_contract_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"success": True}, headers={"Set-Cookie": "session=x"})
        if request.url.path == "/api/csrf-token":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceAdminError) as exc:
            await outlook_plus.build(client).delete_item(BASE_URL, PASSWORD, "user1@outlook.com")
    assert exc.value.code == "SOURCE_CONTRACT"
    assert exc.value.code in SOURCE_ERROR_CODES
