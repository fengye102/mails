"""别名写服务测试：锁死「Apple Account 优先 → iCloud Web 回退」的取舍与状态回写。

真机上这两条链接的配额独立（新接口约 20 个/小时，旧接口约 5 个/小时），
但「总容量已满」是账号级限额——回退旧接口同样会失败，所以不能回退。
这几个分支在真机上很难复现，用假上游把取舍钉住。
"""

import base64
import os
from datetime import timedelta

import httpx
import pytest

from icloud_api import alias_service
from icloud_api.accounts import decrypt_json, encrypt_json
from icloud_api.aliases import AliasError, AliasLimitReached, AliasRateLimited
from icloud_api.models import ICloudAccount

WEB_SERVICE_URL = "https://p68-maildomainws.icloud.com/wm/maildomainws"
WEB_COOKIES = {
    "aosappleid": "user@example.com",
    "X-APPLE-WEBAUTH-TOKEN": '"webauth-value"',
}


def _success(**result: object) -> dict:
    return {"success": True, "result": result}


class FakeUpstream:
    """按 host 分流的假上游：account.apple.com 走新接口，其余走 iCloud Web 旧接口。"""

    def __init__(self, *, manage_failure: str = "", limit_reached: bool = False) -> None:
        self.manage_failure = manage_failure  # "" | "auth" | "rate"
        self.limit_reached = limit_reached
        self.apple_requests: list[str] = []
        self.web_requests: list[str] = []

    # ---- 新接口（Apple Account 管理） ----
    def _manage(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.apple_requests.append(f"{request.method} {path}")
        if self.manage_failure == "auth":
            # 401 + authentication failed 文案 → AppleAccountAuthFailed
            return httpx.Response(401, json={"error": {"message": "authentication failed"}})
        if self.manage_failure == "rate":
            return httpx.Response(429, headers={"Retry-After": "3600"}, text="too many requests")
        if self.limit_reached and path == "/account/manage/email/private/add":
            # 命中 _CAPACITY_MARKERS → AliasLimitReached
            return httpx.Response(
                403, json={"error": {"message": "You have reached the maximum number of addresses"}}
            )
        if path == "/bootstrap/portal":
            return httpx.Response(200, json={"timeOutInterval": 30})
        if path == "/account/manage/gs/ws/token":
            return httpx.Response(200, headers={"scnt": "scnt-1"}, json={"timeOutInterval": 30})
        if path == "/account/manage":
            return httpx.Response(200, json={"apiKey": "api-key-1"})
        if path == "/account/manage/email/private/add":
            return httpx.Response(200, json={"emailAddress": "new-apple@icloud.com"})
        if path == "/account/manage/email/private/add/complete":
            return httpx.Response(
                200, json={"emailAddress": "new-apple@icloud.com", "label": "L", "id": "7"}
            )
        if path == "/account/manage/email/private/7.em":
            return httpx.Response(200, json={"emailAddress": "new-apple@icloud.com", "label": "L"})
        return httpx.Response(200, json={})

    # ---- 旧接口（iCloud Web HME） ----
    def _web(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.web_requests.append(f"{request.method} {path}")
        if path.endswith("/validate"):
            return httpx.Response(
                200,
                json={
                    "webservices": {"premiummailsettings": {"url": WEB_SERVICE_URL}},
                    "dsInfo": {"dsid": "1", "primaryEmail": "user@example.com"},
                },
            )
        if path.endswith("/v1/hme/generate"):
            return httpx.Response(200, json=_success(hme="cand@icloud.com"))
        if path.endswith("/v1/hme/reserve"):
            return httpx.Response(200, json=_success(hme={"hme": "new-web@icloud.com"}))
        raise AssertionError(f"unexpected web request: {request.method} {request.url}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        # 新接口横跨两个域：account.apple.com（管理）+ appleid.apple.com（取 scnt 令牌）
        if request.url.host.endswith("apple.com"):
            return self._manage(request)
        return self._web(request)


@pytest.fixture
def key() -> bytes:
    return base64.b64decode(base64.b64encode(os.urandom(32)))


def _account(key: bytes, **overrides: object) -> ICloudAccount:
    account = ICloudAccount(
        name="主号",
        real_email="user@example.com",
        icloud_email="user@icloud.com",
        host="icloud.com",
    )
    account.cookies_enc = encrypt_json(key, WEB_COOKIES)
    account.apple_session_enc = encrypt_json(key, {"host": "icloud.com", "cookies": {"aasp": "s"}})
    for field, value in overrides.items():
        setattr(account, field, value)
    return account


# ---- 用例 ----


@pytest.mark.asyncio
async def test_prefers_apple_account(key: bytes) -> None:
    """新接口可用时走新接口，并回报 interface=apple_account。"""
    upstream = FakeUpstream()
    transport = httpx.MockTransport(upstream)
    account = _account(key)

    outcome = await alias_service.create_alias(account, key, label="我的标签", transport=transport)

    assert outcome.interface == "apple_account"
    assert outcome.email == "new-apple@icloud.com"
    assert upstream.web_requests == [], "新接口成功时不应碰旧接口"


@pytest.mark.asyncio
async def test_saves_apple_session_after_create(key: bytes) -> None:
    """创建后把新接口会话（scnt/apiKey）加密写回账号，供下次复用。"""
    upstream = FakeUpstream()
    transport = httpx.MockTransport(upstream)
    account = _account(key)

    await alias_service.create_alias(account, key, transport=transport)

    saved = decrypt_json(key, account.apple_session_enc)
    assert isinstance(saved, dict)
    assert saved["scnt"] == "scnt-1"
    assert saved["api_key"] == "api-key-1"


@pytest.mark.asyncio
async def test_limit_reached_does_not_fall_back(key: bytes) -> None:
    """总容量已满 → 直接抛出，不回退旧接口（回退也没用，还会白耗旧接口配额）。"""
    upstream = FakeUpstream(limit_reached=True)
    transport = httpx.MockTransport(upstream)
    account = _account(key)

    with pytest.raises(AliasLimitReached):
        await alias_service.create_alias(account, key, transport=transport)


@pytest.mark.asyncio
async def test_falls_back_to_icloud_web_on_auth_failure(key: bytes) -> None:
    """新接口会话失效 → 回退旧接口，并如实回报 interface=icloud_web。"""
    upstream = FakeUpstream(manage_failure="auth")
    transport = httpx.MockTransport(upstream)
    account = _account(key)

    outcome = await alias_service.create_alias(account, key, label="x", transport=transport)

    assert outcome.interface == "icloud_web"
    assert outcome.email == "new-web@icloud.com"
    assert any(p.endswith("/v1/hme/reserve") for p in upstream.web_requests), "应确实走了旧接口"


@pytest.mark.asyncio
async def test_falls_back_to_icloud_web_on_rate_limit(key: bytes) -> None:
    """新接口被限速 → 仍回退旧接口（两接口配额独立，这正是回退的价值）。"""
    upstream = FakeUpstream(manage_failure="rate")
    transport = httpx.MockTransport(upstream)
    account = _account(key)

    outcome = await alias_service.create_alias(account, key, transport=transport)

    assert outcome.interface == "icloud_web"


# ---- 错误码映射 ----

def test_classify_maps_stable_codes() -> None:
    limit = alias_service.classify(AliasLimitReached("满了"))
    assert (limit.code, limit.message) == ("ALIAS_LIMIT_REACHED", "满了")

    rate = alias_service.classify(AliasRateLimited("限速", retry_after=timedelta(hours=1)))
    assert rate.code == "ALIAS_RATE_LIMITED"
    assert rate.retry_after == 3600.0

    other = alias_service.classify(AliasError("BOOM", "别的原因"))
    assert (other.code, other.message) == ("ALIAS_CREATE_FAILED", "别的原因")