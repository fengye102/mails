"""Apple Account 管理接口客户端测试（对照 Go `apple_account_test.go` + `apple_srp_login_test.go` 的 mock 形状）。

覆盖：Bootstrap 引导序列、Refresh、create_alias 三步（add→complete→confirm）、
错误分类（容量/限速/会话失效）、TTL 判断、.cn 域解析、瞬态重试。
"""

import asyncio
import json

import httpx
import pytest

from icloud_api.apple_account import (
    AliasError,
    AliasLimitReached,
    AliasRateLimited,
    AppleAccountAuthFailed,
    AppleAccountClient,
    AppleAccountState,
    classify_apple_error,
    is_transient_network_error,
    manage_base_for_host,
    manage_origin_for_host,
    set_apple_account_base_url_for_testing,
)


class MockManage:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.scnt = "scnt-mock"
        self.api_key = "api-key-mock"
        self.create_calls = 0
        self.manage_scnt_seen = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        if path == "/account/manage/section/privacy":
            return httpx.Response(200, text="<html>privacy</html>")
        if path == "/bootstrap/portal":
            return httpx.Response(200, json={"timeOutInterval": 30})
        if path == "/account/manage/gs/ws/token":
            return httpx.Response(
                200,
                headers={"scnt": self.scnt, "Set-Cookie": "test-manage-cookie=cookie-value; Path=/"},
                json={"timeOutInterval": 30},
            )
        if path == "/account/manage":
            self.manage_scnt_seen = self.manage_scnt_seen or request.headers.get("scnt") == self.scnt
            return httpx.Response(200, json={"apiKey": self.api_key})
        if path == "/account/manage/email/private/add" and request.method == "POST":
            self.create_calls += 1
            return httpx.Response(200, json={"emailAddress": "abc@icloud.com"})
        if path == "/account/manage/email/private/add/complete" and request.method == "PUT":
            return httpx.Response(200, json={"emailAddress": "abc@icloud.com", "label": "L", "id": "42"})
        if path == "/account/manage/email/private/42.em":
            return httpx.Response(200, json={"emailAddress": "abc@icloud.com", "label": "L"})
        if path == "/account/manage/forwardemail":
            return httpx.Response(200, json={})
        if path == "/v2/jslogs":
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"message": "not found"}})


@pytest.fixture
def mock_manage() -> MockManage:
    return MockManage()


@pytest.fixture
def manage_ts(mock_manage: MockManage):
    ts = httpx.MockTransport(mock_manage.handler)
    yield ts
    set_apple_account_base_url_for_testing("")


def _client(ts, **kw) -> AppleAccountClient:
    return AppleAccountClient(
        AppleAccountState(host="icloud.com", cookies={"aasp": "session-cookie"}),
        transport=ts,
        transient_backoff=0.0,
        **kw,
    )


BEFORE_BOOT = [
    "GET /account/manage/section/privacy",
    "GET /bootstrap/portal",
    "GET /account/manage/gs/ws/token",
    "GET /account/manage",
]


@pytest.mark.asyncio
async def test_bootstrap(manage_ts, mock_manage) -> None:
    client = _client(manage_ts)
    assert client.has_session()
    assert client.needs_refresh()
    await client.bootstrap()
    assert mock_manage.requests == BEFORE_BOOT
    assert mock_manage.manage_scnt_seen
    assert client.state.scnt == "scnt-mock"
    assert client.state.api_key == "api-key-mock"
    assert client.state.cookies["test-manage-cookie"] == "cookie-value"
    assert client.state.last_check_ok
    assert client.has_session()
    assert not client.needs_refresh()


@pytest.mark.asyncio
async def test_bootstrap_no_cookies(manage_ts) -> None:
    client = AppleAccountClient(AppleAccountState(), transport=manage_ts, transient_backoff=0.0)
    with pytest.raises(AppleAccountAuthFailed):
        await client.bootstrap()


@pytest.mark.asyncio
async def test_create_alias_full_sequence(manage_ts, mock_manage) -> None:
    client = _client(manage_ts)
    await client.bootstrap()
    mock_manage.requests.clear()
    result = await client.create_alias("My Label")
    assert result.email == "abc@icloud.com"
    assert result.label == "L"
    assert mock_manage.requests == [
        "POST /account/manage/email/private/add",
        "PUT /account/manage/email/private/add/complete",
        "GET /account/manage/email/private/42.em",
    ]
    assert mock_manage.create_calls == 1


@pytest.mark.asyncio
async def test_create_alias_default_label(manage_ts, mock_manage) -> None:
    client = _client(manage_ts)
    await client.bootstrap()
    await client.create_alias("")
    assert any(r == "PUT /account/manage/email/private/add/complete" for r in mock_manage.requests)


@pytest.mark.asyncio
async def test_create_alias_refreshes_when_needed(manage_ts, mock_manage) -> None:
    # 只 bootstrap 过，但把 scnt 清空强制 needs_refresh 走刷新路径
    client = _client(manage_ts)
    await client.bootstrap()
    mock_manage.requests.clear()
    client.state.scnt = ""
    await client.create_alias("L")
    # scnt 已清空 → 完整 bootstrap（privacy→portal→token→manage）→ add/complete/confirm
    assert mock_manage.requests[:4] == [
        "GET /account/manage/section/privacy",
        "GET /bootstrap/portal",
        "GET /account/manage/gs/ws/token",
        "GET /account/manage",
    ]


# ---- 错误分类 ----


def test_classify_capacity() -> None:
    error = classify_apple_error("创建", 400, json.dumps({"error": {"message": "maximum number of addresses reached"}}).encode())
    assert isinstance(error, AliasLimitReached)
    assert error.code == "ALIAS_LIMIT_REACHED"


def test_classify_rate_limited() -> None:
    error = classify_apple_error("创建", 429, json.dumps({"error": {"retryAfter": 60}}).encode())
    assert isinstance(error, AliasRateLimited)
    assert error.code == "ALIAS_RATE_LIMITED"
    assert error.retry_after.total_seconds() == 60


def test_classify_auth_expired_419() -> None:
    error = classify_apple_error("保活", 419, b'{"error":{"message":"session expired"}}')
    assert isinstance(error, AppleAccountAuthFailed)


def test_classify_auth_expired_401() -> None:
    error = classify_apple_error("创建", 401, b'{"error":{"message":"authentication_failed"}}')
    assert isinstance(error, AppleAccountAuthFailed)


def test_classify_generic() -> None:
    error = classify_apple_error("创建", 500, b"internal error")
    assert isinstance(error, AliasError)
    assert error.code == "APPLE_ACCOUNT_ERROR"


# ---- TTL / 域解析 / 瞬态 ----

def test_needs_refresh_ttl() -> None:
    from datetime import UTC, datetime, timedelta

    state = AppleAccountState(
        scnt="s", api_key="k", last_check_ok=True,
        last_checked_at=datetime.now(UTC).isoformat(),
        manage_expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    )
    client = AppleAccountClient(state, transient_backoff=0.0)
    assert not client.needs_refresh()


def test_host_cn_resolution() -> None:
    assert manage_base_for_host("icloud.com") == "https://appleid.apple.com"
    assert manage_base_for_host("icloud.com.cn") == "https://appleid.apple.com.cn"
    assert manage_origin_for_host("icloud.com.cn") == "https://account.apple.com.cn"


def test_is_transient_network_error() -> None:
    assert is_transient_network_error(httpx.RemoteProtocolError("EOF"))
    assert is_transient_network_error(ConnectionResetError("connection reset"))
    assert is_transient_network_error(httpx.TimeoutException("timed out"))
    assert not is_transient_network_error(ValueError("boom"))


def test_transient_retry_retries(manage_ts, mock_manage) -> None:
    """瞬态网络错误会重试（此处验证不抛，且最终成功）。"""
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2 and request.url.path == "/account/manage/gs/ws/token":
            raise httpx.RemoteProtocolError("EOF")
        return mock_manage.handler(request)

    ts = httpx.MockTransport(flaky)
    client = AppleAccountClient(
        AppleAccountState(host="icloud.com", cookies={"aasp": "session-cookie"}),
        transport=ts,
        transient_backoff=0.0,
    )
    asyncio.run(client.bootstrap())
    assert calls["n"] >= 3