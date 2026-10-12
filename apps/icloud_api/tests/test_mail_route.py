"""D3 收信路由：`GET /icloud-api/v1/accounts/{id}/mail`（假 IMAP 服务器端到端）。

预检类失败（没配 iCloud 邮箱 / 没 App Password / 登录失败）不依赖连接，
走默认 `client` 即可；成功路径必须注入假 IMAP 工厂，否则会连真实 imap.mail.me.com。
"""

import pytest
from fake_imap import FakeImapServer

from icloud_api.models import ICloudAccount


@pytest.fixture
def client_with_imap(settings, engine, validator, fake_imap_factory):
    """带假 IMAP 工厂的 client 工厂：`client = client_with_imap(server)`。"""
    from fastapi.testclient import TestClient

    from icloud_api.app import create_app

    def make(server: FakeImapServer):
        app = create_app(
            settings,
            engine,
            cookie_validator=validator,
            imap_client_factory=fake_imap_factory(server),
        )
        return TestClient(app)

    return make


def _make_account(client, db_engine, *, icloud_email: str = "") -> str:
    """建账号并返回 id。用 admin 接口加密 App Password，icloud_email 直改库。"""
    from sqlalchemy.orm import Session

    response = client.post(
        "/icloud-api/admin/accounts",
        headers={"Authorization": "Bearer operator-test-token"},
        json={"name": "mail-test", "app_password": "app-password-123"},
    )
    assert response.status_code == 200, response.text
    account_id = response.json()["id"]
    if icloud_email:
        with Session(db_engine) as session:
            account = session.get(ICloudAccount, account_id)
            account.icloud_email = icloud_email
            session.commit()
    return account_id


def _seed_messages() -> list[tuple[int, str, str, str, str]]:
    return [
        (1, "github@example.com", "alias1@icloud.com", "Your code is 741233", "code 741233 body"),
        (2, "other@example.com", "someone@gmail.com", "Newsletter", "unrelated body"),
    ]


def test_mail_requires_auth(client) -> None:
    assert client.get("/icloud-api/v1/accounts/whatever/mail").status_code == 401


def test_mail_unknown_account_is_404(client) -> None:
    response = client.get(
        "/icloud-api/v1/accounts/missing/mail", headers={"Authorization": "Bearer operator-test-token"}
    )
    assert response.status_code == 404


def test_mail_without_icloud_email_is_400(client, engine) -> None:
    account_id = _make_account(client, engine)
    server = FakeImapServer(_seed_messages()).start()
    try:
        response = client.get(
            f"/icloud-api/v1/accounts/{account_id}/mail",
            headers={"Authorization": "Bearer operator-test-token"},
            params={"address": "alias1@icloud.com"},
        )
        assert response.status_code == 400
        assert "iCloud 邮箱" in response.json()["detail"]
    finally:
        server.stop()


def test_mail_without_app_password_is_400(client, engine) -> None:
    from sqlalchemy.orm import Session

    account_id = _make_account(client, engine, icloud_email="user@icloud.com")
    with Session(engine) as session:
        account = session.get(ICloudAccount, account_id)
        account.app_password_enc = b""
        session.commit()
    response = client.get(
        f"/icloud-api/v1/accounts/{account_id}/mail",
        headers={"Authorization": "Bearer operator-test-token"},
    )
    assert response.status_code == 400
    assert "App 专用密码" in response.json()["detail"]


def test_mail_login_failure_maps_to_401(client, engine) -> None:
    account_id = _make_account(client, engine, icloud_email="user@icloud.com")
    # 固定凭据的假服务器：账号里存的密码对不上 → IMAP_LOGIN_FAILED。
    server = FakeImapServer(_seed_messages(), username="user@icloud.com", password="different").start()
    try:
        response = client.get(
            f"/icloud-api/v1/accounts/{account_id}/mail",
            headers={"Authorization": "Bearer operator-test-token"},
            params={"address": "alias1@icloud.com"},
        )
        assert response.status_code == 401
        assert "登录失败" in response.json()["detail"]
    finally:
        server.stop()


def test_mail_lists_inbox_without_address(client_with_imap, engine) -> None:
    server = FakeImapServer(_seed_messages(), accept_any_login=True).start()
    client = client_with_imap(server)
    try:
        account_id = _make_account(client, engine, icloud_email="user@icloud.com")
        response = client.get(
            f"/icloud-api/v1/accounts/{account_id}/mail",
            headers={"Authorization": "Bearer operator-test-token"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["method"] == "imap"
        assert body["count"] == 2
        subjects = {message["subject"] for message in body["messages"]}
        assert subjects == {"Your code is 741233", "Newsletter"}
        first = body["messages"][0]
        assert "snippet" in first and "sender" in first
    finally:
        server.stop()


def test_mail_filters_by_alias_address(client_with_imap, engine) -> None:
    server = FakeImapServer(_seed_messages(), accept_any_login=True).start()
    client = client_with_imap(server)
    try:
        account_id = _make_account(client, engine, icloud_email="user@icloud.com")
        response = client.get(
            f"/icloud-api/v1/accounts/{account_id}/mail",
            headers={"Authorization": "Bearer operator-test-token"},
            params={"address": "alias1@icloud.com"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["method"] == "imap_recipient_search"
        assert body["count"] == 1
        assert body["messages"][0]["to"] == "alias1@icloud.com"
        assert "741233" in body["messages"][0]["snippet"]
    finally:
        server.stop()


def test_mail_limit_and_days_are_validated(client, engine) -> None:
    account_id = _make_account(client, engine, icloud_email="user@icloud.com")
    headers = {"Authorization": "Bearer operator-test-token"}
    assert (
        client.get(
            f"/icloud-api/v1/accounts/{account_id}/mail",
            headers=headers,
            params={"limit": 0},
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/icloud-api/v1/accounts/{account_id}/mail",
            headers=headers,
            params={"limit": 999},
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/icloud-api/v1/accounts/{account_id}/mail",
            headers=headers,
            params={"days": -1},
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/icloud-api/v1/accounts/{account_id}/mail",
            headers=headers,
            params={"days": 400},
        ).status_code
        == 422
    )
