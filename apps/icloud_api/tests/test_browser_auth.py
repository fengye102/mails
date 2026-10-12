"""浏览器授权：一次性授权码、单次消费、过期、上游校验失败、落库与脱敏。"""

import base64
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from icloud_api.accounts import account_cookies, resolve_key
from icloud_api.browser_auth import (
    AUTHORIZATION_CODE_PREFIX,
    BROWSER_AUTH_TTL,
    BrowserAuthError,
    complete_browser_auth,
    derive_icloud_email,
    purge_expired,
    start_browser_auth,
)
from icloud_api.hme_client import AccountInfo
from icloud_api.models import ICloudAccount, ICloudBrowserAuthSession

ADMIN = "/icloud-api/admin"
PUBLIC_COMPLETE = "/icloud-api/public/browser-auth/complete"
AUTH = {"Authorization": "Bearer operator-test-token"}


def now() -> datetime:
    return datetime.now(UTC)


def create_account(client, **payload) -> dict:
    body = {"name": "主账号", **payload}
    response = client.post(f"{ADMIN}/accounts", json=body, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def start(client, account_id: str) -> dict:
    response = client.post(f"{ADMIN}/accounts/{account_id}/browser-auth/start", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def complete(client, code: str, cookies: dict | None = None) -> tuple[int, dict]:
    response = client.post(
        PUBLIC_COMPLETE,
        json={"authorization_code": code, "cookies": cookies if cookies is not None else {"aosappleid": "x"}},
    )
    return response.status_code, response.json()


# ---- 发起与查询 ----


def test_start_returns_prefixed_one_time_code(client) -> None:
    account = create_account(client)
    started = start(client, account["id"])
    assert started["authorization_code"].startswith(AUTHORIZATION_CODE_PREFIX)
    assert started["request_id"]
    assert started["expires_at"]


def test_start_stores_only_the_hash(client, engine) -> None:
    account = create_account(client)
    started = start(client, account["id"])
    with Session(engine) as session:
        row = session.scalars(select(ICloudBrowserAuthSession)).one()
    assert row.token_hash != started["authorization_code"]
    assert started["authorization_code"].removeprefix(AUTHORIZATION_CODE_PREFIX) not in row.token_hash
    assert len(row.token_hash) == 64
    assert row.status == "pending"
    assert row.kind == "icloud"


def test_start_rejects_unknown_account(client) -> None:
    response = client.post(f"{ADMIN}/accounts/does-not-exist/browser-auth/start", headers=AUTH)
    assert response.status_code == 404


def test_admin_routes_require_operator_token(client) -> None:
    response = client.post(f"{ADMIN}/accounts", json={"name": "x"})
    assert response.status_code == 401


def test_status_reports_pending_then_completed(client) -> None:
    account = create_account(client)
    started = start(client, account["id"])
    pending = client.get(
        f"{ADMIN}/accounts/{account['id']}/browser-auth/status",
        params={"request_id": started["request_id"]},
        headers=AUTH,
    )
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"

    status_code, _ = complete(client, started["authorization_code"], {"aosappleid": "x"})
    assert status_code == 200
    done = client.get(
        f"{ADMIN}/accounts/{account['id']}/browser-auth/status",
        params={"request_id": started["request_id"]},
        headers=AUTH,
    ).json()
    assert done["request_id"] == started["request_id"]
    assert done["status"] == "completed"
    assert done["error"] == ""
    assert done["expires_at"]


def test_status_scoped_to_account(client) -> None:
    account = create_account(client)
    other = create_account(client, name="另一个")
    started = start(client, account["id"])
    response = client.get(
        f"{ADMIN}/accounts/{other['id']}/browser-auth/status",
        params={"request_id": started["request_id"]},
        headers=AUTH,
    )
    assert response.status_code == 404


def test_status_unknown_request_id(client) -> None:
    account = create_account(client)
    response = client.get(
        f"{ADMIN}/accounts/{account['id']}/browser-auth/status",
        params={"request_id": "nope"},
        headers=AUTH,
    )
    assert response.status_code == 404


# ---- 回填 ----


def test_complete_stores_merged_cookies_and_identity(client, engine, data_key, validator) -> None:
    account = create_account(client)
    started = start(client, account["id"])
    status_code, body = complete(
        client, started["authorization_code"], {"aosappleid": "user@example.com", "X-APPLE-WEBAUTH-TOKEN": "old"}
    )
    assert status_code == 200, body
    assert body["account_id"] == account["id"]
    # 上游 Set-Cookie 刷新的 token 也要落库（Go 保存的是合并后的集合）。
    assert body["cookies_count"] == 2
    assert validator.seen == [{"aosappleid": "user@example.com", "X-APPLE-WEBAUTH-TOKEN": "old"}]

    with Session(engine) as session:
        row = session.get(ICloudAccount, account["id"])
        cookies = account_cookies(row, resolve_key(base64.b64encode(data_key).decode("ascii")))
        assert cookies == {"aosappleid": "user@example.com", "X-APPLE-WEBAUTH-TOKEN": "refreshed"}
        assert row.status == "active"
        assert row.last_error == ""
        assert row.last_validated is not None
        assert row.real_email == "user@example.com"
        assert row.icloud_email == "alias@icloud.com"
        assert row.cookies_enc and b"user@example.com" not in row.cookies_enc


def test_complete_is_single_use(client) -> None:
    account = create_account(client)
    started = start(client, account["id"])
    assert complete(client, started["authorization_code"], {"a": "1"})[0] == 200
    status_code, body = complete(client, started["authorization_code"], {"a": "1"})
    assert status_code == 401
    assert "无效或已过期" in body["detail"]


def test_complete_rejects_unknown_code(client) -> None:
    status_code, _ = complete(client, "iba_not-a-real-code", {"a": "1"})
    assert status_code == 401


def test_complete_rejects_empty_and_oversized_cookie_sets(client) -> None:
    account = create_account(client)
    started = start(client, account["id"])
    assert complete(client, started["authorization_code"], {})[0] == 400
    too_many = {f"c{index}": "v" for index in range(201)}
    assert complete(client, started["authorization_code"], too_many)[0] == 400
    # 两次非法提交都不该消费掉授权码。
    assert complete(client, started["authorization_code"], {"a": "1"})[0] == 200


def test_complete_records_upstream_failure_and_burns_code(client) -> None:
    account = create_account(client)
    started = start(client, account["id"])
    client.app.state.cookie_validator.error = RuntimeError("Cookie 校验失败: 未开通 iCloud+")
    status_code, body = complete(client, started["authorization_code"], {"a": "1"})
    assert status_code == 400
    assert "会话校验失败" in body["detail"]

    status_response = client.get(
        f"{ADMIN}/accounts/{account['id']}/browser-auth/status",
        params={"request_id": started["request_id"]},
        headers=AUTH,
    )
    assert status_response.json()["status"] == "error"
    assert "未开通 iCloud+" in status_response.json()["error"]

    # 失败的码不能再用。
    client.app.state.cookie_validator.error = None
    assert complete(client, started["authorization_code"], {"a": "1"})[0] == 401


def test_complete_marks_processing_before_validating(client, engine) -> None:
    """并发保护：校验上游之前状态必须已离开 pending（用另一个连接读库验证）。"""
    account = create_account(client)
    started = start(client, account["id"])
    observed: list[str] = []

    async def validator(cookies, _account):
        with Session(engine) as probe:
            observed.append(probe.get(ICloudBrowserAuthSession, started["request_id"]).status)
        return cookies, AccountInfo(apple_id="user@example.com")

    client.app.state.cookie_validator = validator
    assert complete(client, started["authorization_code"], {"a": "1"})[0] == 200
    assert observed == ["processing"]


def test_complete_ignores_apple_account_bootstrap_support(client) -> None:
    account = create_account(client)
    started = start(client, account["id"])
    status_code, body = complete(client, started["authorization_code"], {"a": "1"})
    assert status_code == 200
    # 引导还没落地：如实报不支持，而不是假装启用。
    assert body["apple_account"] is False
    assert body["apple_account_supported"] is False
    assert "尚未落地" in body["apple_account_msg"]


def test_complete_enables_apple_account_when_bootstrapper_succeeds(settings, engine, validator) -> None:
    from fastapi.testclient import TestClient

    from icloud_api.app import create_app

    async def bootstrap(cookies: dict[str, str]) -> bool:
        return len(cookies) > 0

    app = create_app(settings, engine, cookie_validator=validator, apple_account_bootstrapper=bootstrap)
    with TestClient(app) as test_client:
        account = create_account(test_client)
        started = start(test_client, account["id"])
        status_code, body = complete(test_client, started["authorization_code"], {"a": "1"})
    assert status_code == 200
    assert body["apple_account"] is True
    assert body["apple_account_supported"] is True
    assert "20 个/小时" in body["apple_account_msg"]


def test_complete_survives_bootstrapper_failure(settings, engine, validator) -> None:
    from fastapi.testclient import TestClient

    from icloud_api.app import create_app

    async def bootstrap(cookies: dict[str, str]) -> bool:
        raise RuntimeError("门户预热失败")

    app = create_app(settings, engine, cookie_validator=validator, apple_account_bootstrapper=bootstrap)
    with TestClient(app) as test_client:
        account = create_account(test_client)
        started = start(test_client, account["id"])
        status_code, body = complete(test_client, started["authorization_code"], {"a": "1"})
    # 引导失败不阻塞授权：旧接口照常可用。
    assert status_code == 200
    assert body["apple_account"] is False
    assert body["apple_account_supported"] is True


# ---- 过期与清理 ----


def test_expired_code_is_rejected(client, engine) -> None:
    account = create_account(client)
    started = start(client, account["id"])
    with Session(engine) as session:
        row = session.get(ICloudBrowserAuthSession, started["request_id"])
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert complete(client, started["authorization_code"], {"a": "1"})[0] == 401


def test_expired_sessions_are_purged_on_next_start(client, engine) -> None:
    account = create_account(client)
    started = start(client, account["id"])
    with Session(engine) as session:
        row = session.get(ICloudBrowserAuthSession, started["request_id"])
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()
        assert purge_expired(session) == 1
        session.commit()
    with Session(engine) as session:
        assert session.scalars(select(ICloudBrowserAuthSession)).all() == []


def test_ttl_is_five_minutes(client, engine) -> None:
    account = create_account(client)
    before = datetime.now(UTC)
    started = start(client, account["id"])
    with Session(engine) as session:
        row = session.get(ICloudBrowserAuthSession, started["request_id"])
    delta = row.expires_at.replace(tzinfo=UTC) - before
    assert timedelta(minutes=5) <= delta <= timedelta(minutes=5, seconds=30)
    assert BROWSER_AUTH_TTL == timedelta(minutes=5)


def test_foreign_key_to_account_is_enforced(engine) -> None:
    with Session(engine) as session:
        session.add(ICloudBrowserAuthSession(account_id="ghost", token_hash="x" * 64, expires_at=now()))
        with pytest.raises(IntegrityError):
            session.commit()


# ---- 账号管理 ----


def test_account_view_is_redacted(client) -> None:
    account = create_account(client, app_password="abcd-efgh-ijkl-mnop")
    assert account["has_app_password"] is True
    assert "app_password" not in account
    assert "cookies_enc" not in account
    response = client.get(f"{ADMIN}/accounts/{account['id']}", headers=AUTH)
    assert response.json()["has_cookies"] is False
    assert response.json()["has_apple_session"] is False


def test_account_delete_requires_confirm(client, engine) -> None:
    account = create_account(client)
    started = start(client, account["id"])
    assert client.request("DELETE", f"{ADMIN}/accounts/{account['id']}", json={}, headers=AUTH).status_code == 400

    response = client.request(
        "DELETE", f"{ADMIN}/accounts/{account['id']}", json={"confirm": True}, headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert body["removed_sessions"] == 1
    # 上游别名不归我们删，如实标注。
    assert body["upstream_aliases_untouched"] is True
    with Session(engine) as session:
        assert session.get(ICloudAccount, account["id"]) is None
        assert session.get(ICloudBrowserAuthSession, started["request_id"]) is None


def test_delete_unknown_account(client) -> None:
    response = client.request("DELETE", f"{ADMIN}/accounts/nope", json={"confirm": True}, headers=AUTH)
    assert response.status_code == 404


# ---- 派生 iCloud 地址 ----


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (AccountInfo(apple_id="a@example.com", primary_email="p@icloud.com"), "p@icloud.com"),
        (AccountInfo(apple_id="me@me.com", primary_email="p@example.com"), "me@me.com"),
        (AccountInfo(apple_id="me@mac.com"), "me@mac.com"),
        (AccountInfo(apple_id="fengye@example.com"), "fengye@icloud.com"),
        (AccountInfo(primary_email="only@example.com"), "only@example.com"),
        (AccountInfo(), ""),
    ],
)
def test_derive_icloud_email(info: AccountInfo, expected: str) -> None:
    assert derive_icloud_email(info) == expected


# ---- 直接调用服务层（不经过 HTTP） ----


@pytest.mark.asyncio
async def test_complete_service_layer_rejects_missing_session(engine, data_key) -> None:
    async def validate(_cookies, _account):  # pragma: no cover - 不该被调用
        raise AssertionError("validate must not run for an unknown authorization code")

    with Session(engine) as session:
        with pytest.raises(BrowserAuthError) as info:
            await complete_browser_auth(
                session,
                authorization_code="iba_missing",
                cookies={"a": "1"},
                data_key=data_key,
                validate=validate,
            )
        assert info.value.code == "INVALID_AUTHORIZATION_CODE"


def test_start_service_layer_requires_existing_account(engine) -> None:
    with Session(engine) as session:
        ghost = ICloudAccount(id="ghost")
        with pytest.raises(BrowserAuthError) as info:
            start_browser_auth(session, ghost)
        assert info.value.code == "ACCOUNT_NOT_FOUND"
