"""统一收件箱「上游实时读信」路由的契约测试。

实时读信不登记邮箱：直接用连接凭据向来源读某个地址的邮件，把三家来源
（cloudflare_temp_email / outlook_plus / cymail）的读契约收敛成同一份 JSON
（``provider_key``/``source_kind``/``sender``/``subject``/``snippet``/``received_at``/
``verification_code``），验证码在服务端提取，正文一律纯文本。

上游 mock 严格复刻真实契约（依据见 skill mails-stack-deployment）：

- CF worker：``GET /admin/mails?address=&limit=&offset=`` →
  ``{"results":[{id,address,created_at,raw}],"count":n}``；单封 ``GET /admin/mails/<id>``；
  凭据走 ``X-Admin-Auth``。
- OEP：``GET /api/external/messages?email=&folder=&skip=&top=`` →
  ``{"success":true,"code":"OK","data":{"emails":[...]}}``；单封
  ``GET /api/external/messages/<id>`` 的 ``data.content`` 实测是**整页 HTML**，
  收件人看 ``to_address``。
- CYMail：``GET /api/inbox?account_id=&alias=&limit=&days=`` →
  ``{"success":true,"data":{account_id,alias,method,messages:[...]}}``；
  CYMail 没有单封正文接口，只有 ``preview``。

这些测试只走 MockTransport，不触碰任何真实上游。
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from mails_api.app import create_app
from mails_api.models import Base, ProviderConnection, User
from mails_api.security.passwords import hash_password
from mails_api.settings import Settings

KEY = bytes(range(32))
PUBLIC = "http://localhost:5173"
ADDRESS = "alias@icloud.com"
HTML_BODY = (
    "<!DOCTYPE html><html><head><style>p{color:red}</style>"
    "<script>alert(1)</script></head>"
    "<body><p>验证码：&nbsp;654321</p>"
    '<p><a href="https://openxlab.org.cn/activate?token=abc">Activate</a></p>'
    "<div>请勿转发</div></body></html>"
)


def _raw(subject: str, body: str, message_id: str) -> str:
    return (
        "From: sender@example.com\r\n"
        f"To: {ADDRESS}\r\n"
        f"Subject: {subject}\r\n"
        "Date: Wed, 09 Jul 2026 06:32:10 GMT\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n{body}\r\n"
    )


def _cf_row(mail_id: str, subject: str, body: str, created_at: str) -> dict:
    return {
        "id": mail_id,
        "address": ADDRESS,
        "created_at": created_at,
        "raw": _raw(subject, body, f"{mail_id}@example.com"),
    }


def _ok(data: object) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "code": "OK", "data": data})


def _upstream(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "cf.example":
        assert request.headers["X-Admin-Auth"] == "cf-secret"
        if path == "/health_check":
            return httpx.Response(200, text="OK")
        if path == "/admin/mails":
            return httpx.Response(
                200,
                json={
                    "results": [
                        _cf_row(
                            "cf-1",
                            "Verify",
                            "Verification code: 123456",
                            "2026-07-09T06:32:10Z",
                        ),
                        _cf_row(
                            "cf-2",
                            "Invoice",
                            "Your invoice is ready",
                            "2026-07-08T06:32:10Z",
                        ),
                    ],
                    "count": 2,
                },
            )
        if path == "/admin/mails/cf-1":
            return httpx.Response(
                200,
                json=_cf_row(
                    "cf-1",
                    "Verify",
                    "Verification code: 123456",
                    "2026-07-09T06:32:10Z",
                ),
            )
    if host == "oep.example":
        assert request.headers["X-API-Key"] == "oep-secret"
        if path == "/api/external/health":
            return _ok({"status": "ok", "database": "ok"})
        if path == "/api/external/capabilities":
            return _ok({"features": ["message_list", "message_detail"]})
        if path == "/api/external/messages":
            return _ok(
                {
                    "count": 1,
                    "emails": [
                        {
                            "id": "9",
                            "method": "graph",
                            "from_address": "no-reply@example.com",
                            "subject": "验证码",
                            "content_preview": "请查看正文",
                            "timestamp": 1783499530,
                        }
                    ],
                }
            )
        if path == "/api/external/messages/9":
            return _ok(
                {
                    "id": "9",
                    "method": "graph",
                    "from_address": "no-reply@example.com",
                    "subject": "验证码",
                    "content": HTML_BODY,
                    "html_content": HTML_BODY,
                    "raw_content": "",
                    "to_address": "user@outlook.com",
                    "timestamp": 1783499530,
                }
            )
    if host == "cy.example":
        assert request.headers["X-API-Key"] == "cy-secret"
        if path == "/api/accounts":
            return _ok([])
        if path == "/api/aliases":
            return _ok(
                {
                    "account_id": "acc-1",
                    "aliases": [{"email": ADDRESS, "active": True}],
                }
            )
        if path == "/api/inbox":
            assert request.url.params["account_id"] == "acc-1"
            assert request.url.params["alias"] == ADDRESS
            return _ok(
                {
                    "account_id": "acc-1",
                    "alias": ADDRESS,
                    "method": "web_api",
                    "messages": [
                        {
                            "id": "m-1",
                            "to": ADDRESS,
                            "from": "no-reply@example.com",
                            "subject": "验证码",
                            "preview": "验证码 112233",
                            "date": "2026-07-09T06:32:10Z",
                        }
                    ],
                }
            )
    raise AssertionError(f"unexpected upstream request: {request.method} {request.url}")


_CONNECTIONS = {
    "cloudflare_temp_email": {
        "kind": "cloudflare_temp_email",
        "name": "CF",
        "base_url": "https://cf.example",
        "api_key": "cf-secret",
    },
    "outlook_plus": {
        "kind": "outlook_plus",
        "name": "OEP",
        "base_url": "https://oep.example",
        "api_key": "oep-secret",
    },
    "cymail": {
        "kind": "cymail",
        "name": "CYMail",
        "base_url": "https://cy.example",
        "api_key": "cy-secret",
        "config": {"account_id": "acc-1"},
    },
}


@contextmanager
def _live_env(engine, kind: str):
    """建库、登录、建好一个来源连接；yield (client, connection_id)。"""
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(username="owner", password_hash=hash_password("secret")))
        db.commit()
    settings = Settings(
        database_url="sqlite://",
        encryption_key=base64.b64encode(KEY).decode(),
        public_origin=PUBLIC,
        upstream_origins=["https://cf.example", "https://oep.example", "https://cy.example"],
    )
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(_upstream))
    with TestClient(create_app(settings, engine, async_client)) as client:
        login = client.post(
            "/api/v1/session",
            headers={"Origin": PUBLIC},
            json={"username": "owner", "password": "secret"},
        )
        assert login.status_code == 200
        created = client.post(
            "/api/v1/connections",
            headers={"Origin": PUBLIC, "X-CSRF-Token": login.json()["csrf_token"]},
            json=_CONNECTIONS[kind],
        )
        assert created.status_code == 201, created.text
        yield client, created.json()["id"]


@pytest.fixture
def cf_env(engine):
    with _live_env(engine, "cloudflare_temp_email") as env:
        yield env


@pytest.fixture
def oep_env(engine):
    with _live_env(engine, "outlook_plus") as env:
        yield env


@pytest.fixture
def cymail_env(engine):
    with _live_env(engine, "cymail") as env:
        yield env


def test_live_mail_lists_cloudflare_messages_with_extracted_code(cf_env):
    client, connection_id = cf_env
    response = client.get(
        f"/api/v1/connections/{connection_id}/live-mail", params={"address": ADDRESS}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["issues"] == []
    newest = body["items"][0]
    assert newest["provider_key"] == "cloudflare:cf-1"
    assert newest["source_kind"] == "cloudflare_temp_email"
    assert newest["sender"] == "sender@example.com"
    assert newest["subject"] == "Verify"
    assert newest["received_at"] == "2026-07-09T06:32:10+00:00"
    assert newest["verification_code"] == "123456"
    assert body["items"][1]["verification_code"] is None


def test_live_mail_filters_by_query_and_paginates(cf_env):
    client, connection_id = cf_env
    filtered = client.get(
        f"/api/v1/connections/{connection_id}/live-mail",
        params={"address": ADDRESS, "query": "invoice"},
    ).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["provider_key"] == "cloudflare:cf-2"

    paged = client.get(
        f"/api/v1/connections/{connection_id}/live-mail",
        params={"address": ADDRESS, "limit": 1, "offset": 1},
    ).json()
    assert paged["total"] == 2
    assert [item["provider_key"] for item in paged["items"]] == ["cloudflare:cf-2"]


def test_live_mail_detail_returns_plain_body_and_code(cf_env):
    client, connection_id = cf_env
    response = client.get(
        f"/api/v1/connections/{connection_id}/live-mail/detail",
        params={"address": ADDRESS, "key": "cloudflare:cf-1"},
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["subject"] == "Verify"
    assert detail["recipient"] == ADDRESS
    assert "Verification code: 123456" in detail["body_text"]
    assert "<" not in detail["body_text"]
    assert detail["verification_code"] == "123456"
    assert "cf-1@example.com" in (detail["rfc_message_id"] or "")


def test_live_mail_detail_unknown_key_is_404(cf_env):
    client, connection_id = cf_env
    response = client.get(
        f"/api/v1/connections/{connection_id}/live-mail/detail",
        params={"address": ADDRESS, "key": "cloudflare:missing"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "LIVE_MESSAGE_NOT_FOUND"


def test_live_mail_converts_outlook_html_body_to_text(oep_env):
    client, connection_id = oep_env
    listed = client.get(
        f"/api/v1/connections/{connection_id}/live-mail", params={"address": "user@outlook.com"}
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["provider_key"] == "graph:9"

    response = client.get(
        f"/api/v1/connections/{connection_id}/live-mail/detail",
        params={"address": "user@outlook.com", "key": "graph:9"},
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["recipient"] == "user@outlook.com"
    assert "<" not in detail["body_text"]
    assert "alert(1)" not in detail["body_text"]
    assert "color:red" not in detail["body_text"]
    assert "654321" in detail["body_text"]
    assert detail["verification_code"] == "654321"
    assert detail["link"] == "https://openxlab.org.cn/activate?token=abc"


def test_live_mail_uses_cymail_account_id_and_preview_body(cymail_env):
    client, connection_id = cymail_env
    listed = client.get(
        f"/api/v1/connections/{connection_id}/live-mail", params={"address": ADDRESS}
    )
    assert listed.status_code == 200, listed.text
    item = listed.json()["items"][0]
    assert item["provider_key"] == "web_api:m-1"
    assert item["verification_code"] == "112233"

    response = client.get(
        f"/api/v1/connections/{connection_id}/live-mail/detail",
        params={"address": ADDRESS, "key": "web_api:m-1"},
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["body_text"] == "验证码 112233"
    assert detail["recipient"] == ADDRESS


def test_live_mail_requires_session(engine):
    with _live_env(engine, "cloudflare_temp_email") as (client, connection_id):
        client.cookies.clear()  # 未登录访问
        response = client.get(
            f"/api/v1/connections/{connection_id}/live-mail", params={"address": ADDRESS}
        )
        assert response.status_code == 401


def test_live_mail_unknown_connection_is_404(cf_env):
    client, _connection_id = cf_env
    response = client.get(
        "/api/v1/connections/2f1c0a3e-0000-4000-8000-000000000000/live-mail",
        params={"address": ADDRESS},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "CONNECTION_NOT_FOUND"


def test_live_mail_inactive_connection_is_502(cf_env, engine):
    client, connection_id = cf_env
    with Session(engine) as db:
        connection = db.get(ProviderConnection, UUID(connection_id))
        connection.status = "error"
        db.commit()
    response = client.get(
        f"/api/v1/connections/{connection_id}/live-mail", params={"address": ADDRESS}
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "UPSTREAM_UNAVAILABLE"


def test_live_mail_surfaces_upstream_failure_as_502(cf_env, monkeypatch):
    client, connection_id = cf_env

    async def broken(*args, **kwargs):
        raise AssertionError("unused")

    from mails_api.providers.base import ProviderError

    def failing_adapter(kind, http_client):
        class Broken:
            async def list_messages(self, *args, **kwargs):
                raise ProviderError("UPSTREAM_TIMEOUT")

            async def get_message(self, *args, **kwargs):
                raise ProviderError("UPSTREAM_TIMEOUT")

        return Broken()

    monkeypatch.setattr("mails_api.routes.live_mail.get_adapter_for", failing_adapter)
    response = client.get(
        f"/api/v1/connections/{connection_id}/live-mail", params={"address": ADDRESS}
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "UPSTREAM_TIMEOUT"
    assert "cf-secret" not in response.text
