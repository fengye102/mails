"""Apple SRP 密码登录（对应 CYMail `internal/hme/apple_srp_login.go`）。

流程（与 Go `AppleSRPLogin` 一致）：
1. 预热 account.apple.com 门户并捕获管理接口 scnt；
2. idmsa 授权初始化（捕获 hashcash 挑战头）；
3. 设备密钥挑战；4. 提交账号；5. SRP 密码派生（s2k/s2k_fo）与签名；
6. 需要 2FA 时通过 `otp_provider` 获取 6 位验证码并提交 + 信任设备；
7. 换取管理接口 scnt + apiKey。

协议细节（端点、请求头、状态码语义、hashcash、指纹头）按 Go 实现照搬，
改动前先对照源文件。所有 HTTP 由注入的 `transport`（httpx.AsyncBaseTransport）
发出，测试用 httpx.MockTransport 模拟 Apple；生产用默认 transport。
"""

import hashlib
import hmac  # noqa: F401  保留：后续 2SV 校验可能用到
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from icloud_api.srp.client import new_srp_client
from icloud_api.srp.params import get_params

# ---- 常量（照搬 apple_srp_login.go / apple_account.go）----

APPLE_ACCOUNT_MANAGE_OAUTH_CLIENT_ID = (
    "af1139274f266b22b68c2a3e7ad932cb3c0bbe854e13a79af78dcc73136882c3"
)
APPLE_HASHCASH_MAX_BITS = 24
APPLE_HASHCASH_MAX_ATTEMPTS = 1 << 24
APPLE_SRP_LOGIN_REQUEST_TIMEOUT = 30.0

IDMSA_AUTH_BASE_URL = "https://idmsa.apple.com/appleauth/auth"
PORTAL_BASE_URL = "https://account.apple.com"
MANAGE_BASE_URL = "https://appleid.apple.com"

APPLE_ACCOUNT_MANAGE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
APPLE_ACCOUNT_MANAGE_REQUEST_CTX = "ca"
APPLE_ACCOUNT_MANAGE_LANGUAGE = "zh"
APPLE_ACCOUNT_MANAGE_TIME_ZONE = "Asia/Shanghai"
APPLE_ACCOUNT_MANAGE_GMT_OFFSET = "GMT+08:00"
APPLE_ACCOUNT_MANAGE_PLATFORM = '"macOS"'
APPLE_ACCOUNT_MANAGE_TZ_OFFSET = 8 * 60 * 60

OTPProvider = Callable[[], Awaitable[str]]


class AppleLoginError(Exception):
    """SRP 登录失败。消息与 Go 一致，便于上层判断分支。"""


# ---- 会话状态（对应 appleSRPLoginSession）----


@dataclass
class _Session:
    username: str
    frame_id: str
    client_id: str
    user_agent: str
    idmsa_base: str
    portal_base: str
    manage_base: str
    transport: httpx.AsyncBaseTransport | None

    cookies: dict[str, str] = field(default_factory=dict)
    scnt: str = ""
    session_id: str = ""
    api_key: str = ""
    auth_attrs: str = ""
    account_country: str = ""
    hc_bits: int = 0
    hc_challenge: str = ""
    complete_hc_bits: int = 0
    complete_hc_challenge: str = ""

    def frame_tag(self) -> str:
        return "auth-" + self.frame_id

    def base_for(self, endpoint: str) -> str:
        if endpoint == "portal":
            return self.portal_base
        if endpoint == "manage":
            return self.manage_base
        return self.idmsa_base

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=APPLE_SRP_LOGIN_REQUEST_TIMEOUT,
            transport=self.transport,
            follow_redirects=False,
        )


def _build_state(session: _Session) -> dict:
    """对应 buildState：返回可直接加密入库的会话状态。"""
    return {
        "cookies": dict(session.cookies),
        "scnt": session.scnt,
        "api_key": session.api_key,
        "session_id": session.session_id,
        "origin": session.portal_base,
        "user_agent": session.user_agent,
        "saved_at": datetime.now(UTC).isoformat(),
    }


# ---- 请求头构造（照搬 srpHeaders / twoFactorHeaders / portalHeaders / manageHeaders）----


def _js_escape(value: str) -> str:
    hex_digits = "0123456789ABCDEF"
    out: list[str] = []
    for ch in value:
        code = ord(ch)
        if (
            "A" <= ch <= "Z"
            or "a" <= ch <= "z"
            or "0" <= ch <= "9"
            or ch in "@*_+-./"
        ):
            out.append(ch)
        elif code <= 0xFF:
            out.append("%" + hex_digits[(code >> 4) & 0xF] + hex_digits[code & 0xF])
        else:
            out.append(
                "%u"
                + hex_digits[(code >> 12) & 0xF]
                + hex_digits[(code >> 8) & 0xF]
                + hex_digits[(code >> 4) & 0xF]
                + hex_digits[code & 0xF]
            )
    return "".join(out)


_FINGERPRINT_ALPHABET = ".0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"
_FINGERPRINT_DICTIONARY = [
    "%20", ";;;", "%3B", "%2C", "und", "fin", "ed;", "%28", "%29", "%3A", "/53",
    "ike", "Web", "0;", ".0", "e;", "on", "il", "ck", "01", "in", "Mo", "fa", "00",
    "32", "la", ".1", "ri", "it", "%u", "le",
]
_FINGERPRINT_CODES: dict[int, tuple[int, int]] = {
    1: (4, 15), 110: (8, 239), 74: (8, 238), 57: (7, 118), 56: (7, 117),
    71: (8, 233), 25: (8, 232), 101: (5, 28), 104: (7, 111), 4: (7, 110),
    105: (6, 54), 5: (7, 107), 109: (7, 106), 103: (9, 423), 82: (9, 422),
    26: (8, 210), 6: (7, 104), 46: (6, 51), 97: (6, 50), 111: (6, 49),
    7: (7, 97), 45: (7, 96), 59: (5, 23), 15: (7, 91), 11: (8, 181),
    72: (8, 180), 27: (8, 179), 28: (8, 178), 16: (7, 88), 88: (10, 703),
    113: (11, 1405), 89: (12, 2809), 107: (13, 5617), 90: (14, 11233),
    42: (15, 22465), 64: (16, 44929), 0: (16, 44928), 81: (9, 350),
    29: (8, 174), 118: (8, 173), 30: (8, 172), 98: (8, 171), 12: (8, 170),
    99: (7, 84), 117: (6, 41), 112: (6, 40), 102: (9, 319), 68: (9, 318),
    31: (8, 158), 100: (7, 78), 84: (6, 38), 55: (6, 37), 17: (7, 73),
    8: (7, 72), 9: (7, 71), 77: (7, 70), 18: (7, 69), 65: (7, 68),
    48: (6, 33), 116: (6, 32), 10: (7, 63), 121: (8, 125), 78: (8, 124),
    80: (7, 61), 69: (7, 60), 119: (7, 59), 13: (8, 117), 79: (8, 116),
    19: (7, 57), 67: (7, 56), 114: (6, 27), 83: (6, 26), 115: (6, 25),
    14: (6, 24), 122: (8, 95), 95: (8, 94), 76: (7, 46), 24: (7, 45),
    37: (7, 44), 50: (5, 10), 51: (5, 9), 108: (6, 17), 22: (7, 33),
    120: (8, 65), 66: (8, 64), 21: (7, 31), 106: (7, 30), 47: (6, 14),
    53: (5, 6), 49: (5, 5), 86: (8, 39), 85: (8, 38), 23: (7, 18),
    75: (7, 17), 20: (7, 16), 2: (5, 3), 73: (8, 23), 43: (9, 45),
    87: (9, 44), 70: (7, 10), 3: (6, 4), 52: (5, 1), 54: (5, 0),
}


def _fingerprint_payload(now: datetime) -> str:
    import zoneinfo

    tz = zoneinfo.ZoneInfo("Asia/Shanghai")
    local = now.astimezone(tz)
    millis = int(local.timestamp() * 1000)
    values: list[str] = ["TF1", "020"]
    values += [""] * 39
    values += [
        "true",
        "true",
        str(millis),
        "-6",
        "6/7/2005, 9:33:44 PM",
        "",
        "",
        "",
        "",
        "",
        "",
        str(millis),
        "0",
        _us_locale_string(local),
    ]
    values += [""] * 34
    values += ["5.6.1-0", ""]
    return "".join(_js_escape(value) + ";" for value in values)


def _us_locale_string(t: datetime) -> str:
    hour = t.hour
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{t.month}/{t.day}/{t.year}, {hour12}:{t.minute:02d}:{t.second:02d} {ampm}"


def _fingerprint_huffman(value: str) -> str | None:
    bits = 0
    buffer = 0
    out: list[str] = []

    def push(width: int, code: int) -> None:
        nonlocal bits, buffer
        buffer = (buffer << width) | code
        bits += width
        while bits >= 6:
            index = (buffer >> (bits - 6)) & 63
            out.append(_FINGERPRINT_ALPHABET[index])
            bits -= 6
            buffer ^= index << bits

    push(6, (len(value) & 7) << 3)
    push(6, (len(value) & 56) | 1)
    for ch in value:
        code = _FINGERPRINT_CODES.get(ord(ch))
        if code is None:
            return None
        push(code[0], code[1])
    terminator = _FINGERPRINT_CODES[0]
    push(terminator[0], terminator[1])
    if bits > 0:
        push(6 - bits, 0)
    return "".join(out)


def _compressed_fingerprint(now: datetime) -> str:
    raw = _fingerprint_payload(now)
    replaced = raw
    for index, token in enumerate(_FINGERPRINT_DICTIONARY):
        replaced = replaced.replace(token, chr(index + 1))
    encoded = _fingerprint_huffman(replaced)
    if encoded is None:
        return raw
    checksum = 0xFFFF
    for byte in raw.encode():
        checksum = ((checksum >> 8) | (checksum << 8)) & 0xFFFF
        checksum ^= byte & 0xFF
        checksum ^= (checksum & 0xFF) >> 4
        checksum ^= (checksum << 12) & 0xFFFF
        checksum ^= ((checksum & 0xFF) << 5) & 0xFFFF
    return (
        encoded
        + _FINGERPRINT_ALPHABET[(checksum >> 12) & 63]
        + _FINGERPRINT_ALPHABET[(checksum >> 6) & 63]
        + _FINGERPRINT_ALPHABET[checksum & 63]
    )


def fd_client_info(user_agent: str) -> str:
    info = {
        "U": user_agent or APPLE_ACCOUNT_MANAGE_USER_AGENT,
        "L": APPLE_ACCOUNT_MANAGE_LANGUAGE,
        "Z": APPLE_ACCOUNT_MANAGE_GMT_OFFSET,
        "V": "1.1",
        "F": _compressed_fingerprint(datetime.now(UTC)),
    }
    return json.dumps(info, separators=(",", ":"))


def _browser_hints(headers: dict[str, str]) -> None:
    headers["Accept-Language"] = APPLE_ACCOUNT_MANAGE_LANGUAGE + ",en;q=0.9"
    headers["Sec-CH-UA"] = '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"'
    headers["Sec-CH-UA-Mobile"] = "?0"
    headers["Sec-CH-UA-Platform"] = APPLE_ACCOUNT_MANAGE_PLATFORM


def _srp_headers(session: _Session) -> dict[str, str]:
    frame_tag = session.frame_tag()
    origin = session.idmsa_base.removesuffix("/appleauth/auth")
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": origin + "/",
        "X-Apple-Widget-Key": session.client_id,
        "X-Apple-OAuth-Client-Id": session.client_id,
        "X-Apple-OAuth-Client-Type": "firstPartyAuth",
        "X-Apple-OAuth-Redirect-URI": session.portal_base,
        "X-Apple-OAuth-Response-Mode": "web_message",
        "X-Apple-OAuth-Response-Type": "code",
        "X-Apple-OAuth-State": frame_tag,
        "X-Apple-Frame-Id": frame_tag,
        "X-Requested-With": "XMLHttpRequest",
        "X-Apple-I-FD-Client-Info": fd_client_info(session.user_agent),
        "X-Apple-Domain-Id": "11",
        "X-Apple-Privacy-Consent": "true",
        "X-Apple-Privacy-Consent-Accepted": "true",
    }
    _browser_hints(headers)
    headers["Sec-Fetch-Dest"] = "empty"
    headers["Sec-Fetch-Mode"] = "cors"
    headers["Sec-Fetch-Site"] = "same-origin"
    if session.auth_attrs:
        headers["X-Apple-Auth-Attributes"] = session.auth_attrs
    if session.scnt:
        headers["scnt"] = session.scnt
    if session.session_id:
        headers["X-Apple-ID-Session-Id"] = session.session_id
    return headers


def _two_factor_headers(session: _Session) -> dict[str, str]:
    headers = _srp_headers(session)
    headers["Accept"] = "application/json, text/plain, */*"
    headers["X-Apple-App-Id"] = session.client_id
    headers.pop("X-Requested-With", None)
    return headers


def _portal_headers(session: _Session, accept: str, json_content: bool) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "Referer": session.portal_base + "/",
        "User-Agent": session.user_agent,
        "Accept-Language": APPLE_ACCOUNT_MANAGE_LANGUAGE + ",en;q=0.9",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Sec-CH-UA-Platform": APPLE_ACCOUNT_MANAGE_PLATFORM,
        "Sec-CH-UA": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "Sec-CH-UA-Mobile": "?0",
    }
    if json_content:
        headers["Content-Type"] = "application/json"
        headers["X-Apple-I-Request-Context"] = APPLE_ACCOUNT_MANAGE_REQUEST_CTX
        headers["X-Apple-I-TimeZone"] = APPLE_ACCOUNT_MANAGE_TIME_ZONE
        headers["X-Apple-I-FD-Client-Info"] = fd_client_info(session.user_agent)
    return headers


def _manage_headers(session: _Session, with_scnt: bool) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": session.portal_base,
        "Referer": session.portal_base + "/",
        "User-Agent": session.user_agent,
        "Accept-Language": APPLE_ACCOUNT_MANAGE_LANGUAGE + ",en;q=0.9",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Sec-CH-UA-Platform": APPLE_ACCOUNT_MANAGE_PLATFORM,
        "Sec-CH-UA": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "Sec-CH-UA-Mobile": "?0",
        "X-Apple-I-FD-Client-Info": fd_client_info(session.user_agent),
        "X-Apple-I-Request-Context": APPLE_ACCOUNT_MANAGE_REQUEST_CTX,
        "X-Apple-I-TimeZone": APPLE_ACCOUNT_MANAGE_TIME_ZONE,
    }
    if with_scnt and session.scnt:
        headers["scnt"] = session.scnt
    if session.api_key:
        headers["X-Apple-Api-Key"] = session.api_key
    return headers


# ---- Cookie / 响应头 / 域切换 ----


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(
        f"{name}={cookies[name]}" for name in sorted(cookies) if name and cookies[name].strip()
    )


class _DomainSwitch(Exception):
    """响应要求切换 iCloud 域，端点已切换，调用方应重试一次。"""


def _parse_domain_redirect(status: int, data: bytes) -> str:
    if not 300 <= status < 400:
        return ""
    try:
        payload = json.loads(data.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if isinstance(payload, dict):
        value = payload.get("domainToUse")
        if isinstance(value, str):
            return value.strip()
    return ""


def _switch_domain(session: _Session, domain_to_use: str) -> bool:
    domain_to_use = domain_to_use.strip().lower()
    to_cn = "apple.com.cn" in domain_to_use or "icloud.com.cn" in domain_to_use
    to_us = not to_cn and ("apple.com" in domain_to_use or "icloud.com" in domain_to_use)
    if not to_cn and not to_us:
        return False
    current = f"{session.idmsa_base}|{session.portal_base}|{session.manage_base}"
    has_cn = "apple.com.cn" in current
    if to_cn == has_cn:
        return False
    source, target = ("apple.com", "apple.com.cn") if to_cn else ("apple.com.cn", "apple.com")
    if source not in current:
        return False
    session.idmsa_base = session.idmsa_base.replace(source, target, 1)
    session.portal_base = session.portal_base.replace(source, target, 1)
    session.manage_base = session.manage_base.replace(source, target, 1)
    return True


def _trim_body(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return "空响应"
    return text[:240] + "..." if len(text) > 240 else text


async def _do(
    session: _Session,
    method: str,
    endpoint: str,
    path: str,
    headers: dict[str, str],
    body: dict | None,
    allow_409: bool,
) -> tuple[int, bytes]:
    """对应 Go do()：域切换自动重试一次。"""
    for _ in range(2):
        try:
            return await _do_once(session, method, endpoint, path, headers, body, allow_409)
        except _DomainSwitch:
            continue
    raise AppleLoginError("Apple 登录域切换后重试仍失败")


async def _do_once(
    session: _Session,
    method: str,
    endpoint: str,
    path: str,
    headers: dict[str, str],
    body: dict | None,
    allow_409: bool,
) -> tuple[int, bytes]:
    url = session.base_for(endpoint).rstrip("/") + path
    request_headers = {"User-Agent": session.user_agent}
    for key, value in headers.items():
        if value.strip():
            request_headers[key] = value
    if body is not None and "Content-Type" not in request_headers:
        request_headers["Content-Type"] = "application/json"
    cookie = _cookie_header(session.cookies)
    if cookie:
        request_headers["Cookie"] = cookie

    async with session.client() as client:
        try:
            response = await client.request(
                method, url, json=body if body is not None else None, headers=request_headers
            )
        except httpx.HTTPError as error:
            raise AppleLoginError(f"Apple 登录网络错误: {error}") from error
        # httpx Cookies: 按名称合并（模拟浏览器行为，与 Go mergeSetCookies 一致）。
        for name, value in response.cookies.items():
            if name and value:
                session.cookies[name] = value
        _update_state_from_headers(session, response.headers)
        data = response.content
        status = response.status_code

    domain = _parse_domain_redirect(status, data)
    if domain:
        if _switch_domain(session, domain):
            raise _DomainSwitch
        raise AppleLoginError(f"Apple 要求切换 iCloud 域, 但不支持的域: {domain}")
    if status == 403:
        raise AppleLoginError("Apple ID 或密码错误, 或当前账号被限制登录")
    if status == 412:
        raise AppleLoginError("需要先在 appleid.apple.com 同意隐私条款")
    if allow_409 and status in (401, 409):
        return status, data
    if not 200 <= status < 300:
        raise AppleLoginError(f"Apple 协议 HTTP {status}: {_trim_body(data)}")
    return status, data


def _update_state_from_headers(session: _Session, headers: httpx.Headers) -> None:
    if value := headers.get("scnt", "").strip():
        session.scnt = value
    if value := headers.get("X-Apple-ID-Session-Id", "").strip():
        session.session_id = value
    if value := headers.get("X-Apple-Auth-Attributes", "").strip():
        session.auth_attrs = value
    if value := headers.get("X-Apple-ID-Account-Country", "").strip():
        session.account_country = value
    if value := headers.get("X-Apple-HC-Bits", "").strip():
        try:
            bits = int(value)
        except ValueError:
            bits = 0
        if bits > 0:
            session.hc_bits = bits
    if value := headers.get("X-Apple-HC-Challenge", "").strip():
        session.hc_challenge = value


# ---- 登录步骤 ----


async def _prime_portal(session: _Session) -> None:
    accept_html = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
        "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    )
    await _do(session, "GET", "portal", "/account/manage/section/privacy",
              _portal_headers(session, accept_html, False), None, False)
    await _do(session, "GET", "portal", "/bootstrap/portal",
              _portal_headers(session, "application/json, text/plain, */*", True), None, False)
    try:
        await _do(session, "GET", "manage", "/account/manage/gs/ws/token",
                  _manage_headers(session, False), None, False)
    except AppleLoginError:
        if not session.scnt.strip():
            raise


async def _auth_start(session: _Session) -> None:
    frame_tag = session.frame_tag()
    path = (
        "/authorize/signin"
        f"?frame_id={frame_tag}&skVersion=7&iframeId={frame_tag}"
        f"&client_id={session.client_id}&redirect_uri={session.portal_base}"
        "&response_type=code&response_mode=web_message"
        f"&state={frame_tag}&authVersion=8.0.2"
    )
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
            "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Referer": session.portal_base + "/",
        "Sec-Fetch-Dest": "iframe",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-site",
    }
    _browser_hints(headers)
    await _do(session, "GET", "idmsa", path, headers, None, False)
    # 记住 complete 阶段使用的 hashcash 挑战（来自 authStart 响应头 X-Apple-HC-*）。
    if (
        session.complete_hc_bits <= 0
        and not session.complete_hc_challenge
        and session.hc_bits > 0
        and session.hc_challenge.strip()
    ):
        session.complete_hc_bits = session.hc_bits
        session.complete_hc_challenge = session.hc_challenge


async def _auth_device_key_challenge(session: _Session) -> None:
    headers = _srp_headers(session)
    headers.pop("scnt", None)
    headers.pop("X-Apple-ID-Session-Id", None)
    headers.pop("X-Apple-App-Id", None)
    await _do(session, "POST", "idmsa", "/verify/device/key/challenge",
              headers, {"passkeyAutofill": False}, False)


async def _auth_federate(session: _Session) -> None:
    await _do(session, "POST", "idmsa", "/federate?isRememberMeEnabled=true",
              _srp_headers(session), {"accountName": session.username, "rememberMe": True}, False)


def _derive_password(password: str, salt: bytes, iterations: int, protocol: str) -> bytes:
    """对应 deriveAppleSRPPassword：s2k 用原始摘要，s2k_fo 用摘要的十六进制字符串。"""
    pass_hash = hashlib.sha256(password.encode()).digest()
    if protocol == "s2k":
        input_bytes = pass_hash
    elif protocol == "s2k_fo":
        input_bytes = pass_hash.hex().encode()
    else:
        raise AppleLoginError(f"不支持的 Apple SRP 协议 {protocol!r}")
    return hashlib.pbkdf2_hmac("sha256", input_bytes, salt, iterations, dklen=32)


def _leading_zero_bits(data: bytes) -> int:
    total = 0
    for byte in data:
        for bit in range(7, -1, -1):
            if byte & (1 << bit):
                return total
            total += 1
    return total


def generate_apple_hashcash(bits: int, challenge: str, now: datetime | None = None) -> str:
    """对应 generateAppleHashcash：SHA-1 前导零比特数 >= bits。"""
    challenge = challenge.strip()
    if bits <= 0 or not challenge:
        raise AppleLoginError("Apple Account 缺少动态验证挑战, 请重新登录")
    if bits > APPLE_HASHCASH_MAX_BITS:
        raise AppleLoginError("Apple Account 动态验证难度过高, 请稍后重试")
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    prefix = f"1:{bits}:{stamp}:{challenge}::"
    for counter in range(APPLE_HASHCASH_MAX_ATTEMPTS):
        value = prefix + _base36(counter)
        if _leading_zero_bits(hashlib.sha1(value.encode()).digest()) >= bits:
            return value
    raise AppleLoginError("Apple Account 动态验证生成失败, 请稍后重试")


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    out = ""
    while value:
        out = digits[value % 36] + out
        value //= 36
    return out


async def _auth_srp(session: _Session, password: str) -> bool:
    """返回 needs2FA（signin/complete 返回 409）。"""
    import dataclasses

    params = dataclasses.replace(get_params(2048), no_user_name_in_x=True)
    client = new_srp_client(params)
    init_body = {
        "a": _b64(client.get_a_bytes()),
        "accountName": session.username,
        "protocols": ["s2k", "s2k_fo"],
    }
    _, data = await _do(session, "POST", "idmsa", "/signin/init",
                        _srp_headers(session), init_body, False)
    init = json.loads(data)
    try:
        server_b = _unb64(init["b"])
        salt = _unb64(init["salt"])
    except (KeyError, ValueError) as error:
        raise AppleLoginError(f"解析 Apple SRP B 失败: {error}") from error
    derived = _derive_password(password, salt, int(init["iteration"]), init["protocol"])
    client.process_client_challenge(session.username.encode(), derived, salt, server_b)

    complete_body = {
        "accountName": session.username,
        "m1": _b64(client.m1),
        "m2": _b64(client.m2),
        "c": init["c"],
        "rememberMe": True,
    }
    headers = _srp_headers(session)
    headers["X-Apple-HC"] = generate_apple_hashcash(
        session.complete_hc_bits, session.complete_hc_challenge
    )
    status, _ = await _do(session, "POST", "idmsa",
                          "/signin/complete?isRememberMeEnabled=true",
                          headers, complete_body, True)
    if status == 401:
        raise AppleLoginError("Apple ID 或密码错误, 请检查后重试")
    return status == 409


async def _handle_two_factor(session: _Session, otp_provider: OTPProvider | None) -> None:
    if otp_provider is None:
        try:
            await _do(session, "GET", "idmsa", "/verify/trusteddevice/challenge",
                      _two_factor_headers(session), None, False)
        except AppleLoginError:
            pass
        raise AppleLoginError("账号启用了双重认证, 需要提供 otpProvider 获取 6 位验证码")
    code = (await otp_provider()).strip()
    if len(code) != 6:
        raise AppleLoginError("2FA 验证码必须是 6 位")
    body = {"securityCode": {"code": code}}
    try:
        status, _ = await _do(session, "POST", "idmsa",
                              "/verify/trusteddevice/securitycode",
                              _two_factor_headers(session), body, False)
    except AppleLoginError as error:
        message = str(error)
        if message.startswith("Apple 协议 HTTP"):
            raise AppleLoginError(f"Apple 2FA 验证失败: {message.split(': ', 1)[1]}") from error
        raise
    if status not in (200, 204):
        raise AppleLoginError(f"Apple 2FA 验证失败: HTTP {status}")
    await _do(session, "GET", "idmsa", "/2sv/trust", _srp_headers(session), None, False)


async def _finish_manage(session: _Session) -> dict:
    await _do(session, "GET", "manage", "/account/manage/gs/ws/token",
              _manage_headers(session, False), None, False)
    if not session.scnt.strip():
        raise AppleLoginError("Apple 登录未返回 scnt, 无法完成登录")
    _, data = await _do(session, "GET", "manage", "/account/manage",
                        _manage_headers(session, True), None, False)
    manage = json.loads(data)
    api_key = (manage.get("apiKey") or "").strip()
    if not api_key:
        raise AppleLoginError("Apple Account 管理接口未返回 api_key")
    session.api_key = api_key
    return _build_state(session)


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def _unb64(value: str) -> bytes:
    import base64

    return base64.b64decode(value)


async def srp_login(
    username: str,
    password: str,
    otp_provider: OTPProvider | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    idmsa_base: str = IDMSA_AUTH_BASE_URL,
    portal_base: str = PORTAL_BASE_URL,
    manage_base: str = MANAGE_BASE_URL,
) -> dict:
    """Apple ID + 密码 SRP 登录，返回 AppleAccountState 形状的 dict（对应 Go AppleSRPLogin）。"""
    username = username.strip().lower()
    if not username or not password.strip():
        raise AppleLoginError("缺少 Apple ID 或密码")
    session = _Session(
        username=username,
        frame_id=str(__import__("uuid").uuid4()).lower(),
        client_id=APPLE_ACCOUNT_MANAGE_OAUTH_CLIENT_ID,
        user_agent=APPLE_ACCOUNT_MANAGE_USER_AGENT,
        idmsa_base=idmsa_base.rstrip("/"),
        portal_base=portal_base.rstrip("/"),
        manage_base=manage_base.rstrip("/"),
        transport=transport,
    )
    await _prime_portal(session)
    await _auth_start(session)
    await _auth_device_key_challenge(session)
    await _auth_federate(session)
    needs_2fa = await _auth_srp(session, password)
    if needs_2fa:
        await _handle_two_factor(session, otp_provider)
    return await _finish_manage(session)
