"""Apple SRP 登录测试：假 idmsa/管理服务验证请求序列（对照 apple_srp_login_test.go）。

mock 不校验 M1/M2 的真实性（固定 B=2），只验证：
- 请求顺序与 Go 一致；
- signin/complete 带 X-Apple-HC 且难度达标；
- 2FA / 密码错误 / 无 OTPProvider 各分支的错误语义。
"""

import base64
import hashlib
import json

import httpx
import pytest

from icloud_api.apple_login import srp_login

MOCK_MANAGE_SCNT = "scnt-final"
MOCK_MANAGE_API_KEY = "api-key-final"


class MockIDMSA:
    """对应 Go mockIDMSAServer。complete_status: 200 无 2FA / 409 要 2FA / 403 密码错。"""

    def __init__(self, complete_status: int, security_code_status: int = 204) -> None:
        self.complete_status = complete_status
        self.security_code_status = security_code_status
        self.requests: list[str] = []
        self.security_codes: list[str] = []
        self.hc_value = ""

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        if path == "/authorize/signin":
            return httpx.Response(
                200,
                headers={
                    "X-Apple-HC-Bits": "4",
                    "X-Apple-HC-Challenge": "mock-challenge-123",
                    "Content-Type": "text/html",
                },
                text="<html>signin</html>",
            )
        if path == "/verify/device/key/challenge":
            return httpx.Response(200, json={})
        if path == "/federate":
            return httpx.Response(200, json={})
        if path == "/signin/init":
            server_b = bytes(255) + bytes([2])
            return httpx.Response(200, json={
                "iteration": 1000,
                "salt": base64.b64encode(b"salt-bytes").decode(),
                "protocol": "s2k",
                "b": base64.b64encode(server_b).decode(),
                "c": "auth-mock-c",
            })
        if path == "/signin/complete":
            self.hc_value = request.headers.get("X-Apple-HC", "")
            if self.complete_status == 403:
                return httpx.Response(403, json={"error": {"message": "invalid credentials"}})
            if self.complete_status == 409:
                return httpx.Response(
                    409,
                    headers={"scnt": "scnt-from-409", "X-Apple-ID-Session-Id": "session-from-409"},
                    json={"error": {"message": "2FA required"}},
                )
            return httpx.Response(200, json={})
        if path == "/verify/trusteddevice/securitycode":
            body = json.loads(request.content)
            self.security_codes.append(body["securityCode"]["code"])
            return httpx.Response(self.security_code_status)
        if path == "/2sv/trust":
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"message": "not found"}})


class MockManage:
    """门户 + appleid 管理 API（对应 Go mockManageServer，两个 base 共用）。"""

    def __init__(self) -> None:
        self.requests: list[str] = []
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
                headers={
                    "scnt": MOCK_MANAGE_SCNT,
                    "Set-Cookie": "test-manage-cookie=cookie-value; Path=/; HttpOnly",
                },
                json={"timeOutInterval": 30},
            )
        if path == "/account/manage":
            self.manage_scnt_seen = self.manage_scnt_seen or request.headers.get("scnt") == MOCK_MANAGE_SCNT
            return httpx.Response(200, json={"apiKey": MOCK_MANAGE_API_KEY})
        return httpx.Response(404, json={})


class MockPair:
    def __init__(self, complete_status: int, security_code_status: int = 204) -> None:
        self.idmsa = MockIDMSA(complete_status, security_code_status)
        self.manage = MockManage()

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            if "idmsa" in host:
                return self.idmsa.handler(request)
            return self.manage.handler(request)

        return httpx.MockTransport(handler)


WANT_IDMSA_2FA = [
    "GET /authorize/signin",
    "POST /verify/device/key/challenge",
    "POST /federate",
    "POST /signin/init",
    "POST /signin/complete",
    "POST /verify/trusteddevice/securitycode",
    "GET /2sv/trust",
]
WANT_MANAGE = [
    "GET /account/manage/section/privacy",
    "GET /bootstrap/portal",
    "GET /account/manage/gs/ws/token",  # prime 阶段
    "GET /account/manage/gs/ws/token",  # finish 阶段
    "GET /account/manage",
]


@pytest.mark.asyncio
async def test_srp_login_with_2fa() -> None:
    pair = MockPair(complete_status=409)
    otp_calls = 0

    async def otp() -> str:
        nonlocal otp_calls
        otp_calls += 1
        return "123456"

    state = await srp_login(
        "Test@Example.com",
        "password123",
        otp,
        transport=pair.transport(),
        idmsa_base="https://mock-idmsa.test",
        portal_base="https://mock-portal.test",
        manage_base="https://mock-manage.test",
    )
    assert state["scnt"] == MOCK_MANAGE_SCNT
    assert state["api_key"] == MOCK_MANAGE_API_KEY
    assert state["session_id"] == "session-from-409"
    assert state["origin"] == "https://mock-portal.test"
    assert state["cookies"]["test-manage-cookie"] == "cookie-value"
    assert state["saved_at"]
    assert otp_calls == 1
    assert pair.idmsa.security_codes == ["123456"]
    # signin/complete 带 X-Apple-HC 且 SHA-1 前导零 >= 4
    assert pair.idmsa.hc_value
    digest = hashlib.sha1(pair.idmsa.hc_value.encode()).digest()
    assert _leading_zero_bits(digest) >= 4
    assert pair.idmsa.requests == WANT_IDMSA_2FA
    assert pair.manage.requests == WANT_MANAGE
    assert pair.manage.manage_scnt_seen


@pytest.mark.asyncio
async def test_srp_login_without_2fa() -> None:
    pair = MockPair(complete_status=200)
    otp_calls = 0

    async def otp() -> str:
        nonlocal otp_calls
        otp_calls += 1
        return "000000"

    state = await srp_login(
        "test@example.com",
        "password123",
        otp,
        transport=pair.transport(),
        idmsa_base="https://mock-idmsa.test",
        portal_base="https://mock-portal.test",
        manage_base="https://mock-manage.test",
    )
    assert otp_calls == 0
    assert state["scnt"] == MOCK_MANAGE_SCNT
    assert state["api_key"] == MOCK_MANAGE_API_KEY
    assert pair.idmsa.requests == WANT_IDMSA_2FA[:5]  # 无 securitycode / 2sv
    assert all("securitycode" not in r and "2sv" not in r for r in pair.idmsa.requests)


@pytest.mark.asyncio
async def test_srp_login_wrong_password() -> None:
    pair = MockPair(complete_status=403)
    with pytest.raises(Exception, match="密码错误"):
        await srp_login(
            "test@example.com",
            "wrong-password",
            transport=pair.transport(),
            idmsa_base="https://mock-idmsa.test",
            portal_base="https://mock-portal.test",
            manage_base="https://mock-manage.test",
        )


@pytest.mark.asyncio
async def test_srp_login_2fa_without_provider() -> None:
    pair = MockPair(complete_status=409)
    with pytest.raises(Exception, match="双重认证"):
        await srp_login(
            "test@example.com",
            "password123",
            None,
            transport=pair.transport(),
            idmsa_base="https://mock-idmsa.test",
            portal_base="https://mock-portal.test",
            manage_base="https://mock-manage.test",
        )


@pytest.mark.asyncio
async def test_srp_login_2fa_failed() -> None:
    pair = MockPair(complete_status=409, security_code_status=400)

    async def otp() -> str:
        return "123456"

    with pytest.raises(Exception, match="2FA 验证失败"):
        await srp_login(
            "test@example.com",
            "password123",
            otp,
            transport=pair.transport(),
            idmsa_base="https://mock-idmsa.test",
            portal_base="https://mock-portal.test",
            manage_base="https://mock-manage.test",
        )


@pytest.mark.asyncio
async def test_srp_login_requires_username_and_password() -> None:
    with pytest.raises(Exception, match="缺少 Apple ID 或密码"):
        await srp_login("", "x")
    with pytest.raises(Exception, match="缺少 Apple ID 或密码"):
        await srp_login("a@icloud.com", "  ")


# ---- hashcash / 指纹单元 ----


def test_hashcash_leading_zero_bits() -> None:
    from datetime import UTC, datetime

    from icloud_api.apple_login import generate_apple_hashcash

    token = generate_apple_hashcash(4, "mock-challenge-123", datetime(2026, 1, 1, tzinfo=UTC))
    assert token.startswith("1:4:20260101000000:mock-challenge-123::")
    assert _leading_zero_bits(hashlib.sha1(token.encode()).digest()) >= 4


def _leading_zero_bits(data: bytes) -> int:
    total = 0
    for byte in data:
        for bit in range(7, -1, -1):
            if byte & (1 << bit):
                return total
            total += 1
    return total
