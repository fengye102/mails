"""Apple Account 管理接口客户端（对应 CYMail `internal/hme/apple_account.go`，1289 行）。

新接口（`appleid.apple.com` / `account.apple.com` 门户）与旧接口（iCloud Web）配额独立，
新接口约 20 个/小时，旧接口约 5 个/小时，所以 `alias_service` 一律先走这里。

协议要点（照搬 Go 实现，改动前先对照源文件）：
- 会话由「浏览器采集的 Cookie」引导：门户预热 → `gs/ws/token` 捕获 scnt → `/account/manage` 取 apiKey；
- 风控基于 scnt / X-Apple-Api-Key / X-Apple-I-FD-Client-Info 等请求头，无需 TLS 指纹；
- 419 是会话超时；401/403 只有在响应体像鉴权失效时才归为会话失效；
- 瞬态网络错误（EOF/超时/DNS/连接重置）重试 3 次，退避 800/1600/2400ms；
  HTTP 非 2xx 与已分类的业务错误立即返回，不重试。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from icloud_api.aliases import (
    AliasError,
    AliasLimitReached,
    AliasRateLimited,
    _parse_retry_after,
)
from icloud_api.apple_login import (
    APPLE_ACCOUNT_MANAGE_LANGUAGE,
    APPLE_ACCOUNT_MANAGE_PLATFORM,
    APPLE_ACCOUNT_MANAGE_REQUEST_CTX,
    APPLE_ACCOUNT_MANAGE_TIME_ZONE,
    APPLE_ACCOUNT_MANAGE_USER_AGENT,
    fd_client_info,
)

APPLE_ACCOUNT_MANAGE_ORIGIN = "https://account.apple.com"
APPLE_ACCOUNT_MANAGE_CN_ORIGIN = "https://account.apple.com.cn"
APPLE_ACCOUNT_MANAGE_CN_BASE = "https://appleid.apple.com.cn"
APPLE_ACCOUNT_MANAGE_BASE = "https://appleid.apple.com"
APPLE_ACCOUNT_MANAGE_ZONE_URL = "https://idmsa.apple.com"  # 占位，勿用

# 会话超时状态码（Apple Account 管理接口专用）。
APPLE_ACCOUNT_HTTP_STATUS_SESSION_TIMEOUT = 419

APPLE_ACCOUNT_REQUEST_TIMEOUT = 30.0
APPLE_ACCOUNT_TRANSIENT_RETRIES = 3
APPLE_ACCOUNT_TRANSIENT_BACKOFF = 0.8
APPLE_ACCOUNT_KEEP_ALIVE_DEFAULT_INTERVAL = timedelta(minutes=4)
APPLE_ACCOUNT_KEEP_ALIVE_FORWARD_EMAIL_PATH = "/account/manage/forwardemail"
APPLE_ACCOUNT_KEEP_ALIVE_JS_LOGS_PATH = "/v2/jslogs"

_SEC_CH_UA = '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"'
_PRIVACY_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
)
_JSON_ACCEPT = "application/json, text/plain, */*"

# 测试可注入的 Base URL 覆盖（生产保持空串，按 state.Host 解析）。
_base_url_override = ""


class AppleAccountAuthFailed(AliasError):
    """Apple Account 管理态会话已失效，需要重新采集浏览器 Cookie。"""

    def __init__(self, message: str = "") -> None:
        super().__init__("APPLE_ACCOUNT_AUTH_FAILED", message)


def set_apple_account_base_url_for_testing(raw_url: str) -> str:
    """重定向管理 API Base URL（仅供测试注入 mock 服务），返回旧值。"""
    global _base_url_override
    previous = _base_url_override
    _base_url_override = raw_url
    return previous


def _is_cn_host(host: str) -> bool:
    h = (host or "").strip().lower()
    return "icloud.com.cn" in h or "appleid.apple.com.cn" in h or "account.apple.com.cn" in h


def manage_base_for_host(host: str) -> str:
    """按域名解析管理 API Base URL：中国大陆域走 .com.cn，其余走 .com。"""
    return APPLE_ACCOUNT_MANAGE_CN_BASE if _is_cn_host(host) else APPLE_ACCOUNT_MANAGE_BASE


def manage_origin_for_host(host: str) -> str:
    """按域名解析门户 Origin：中国大陆域走 .com.cn，其余走 .com。"""
    return APPLE_ACCOUNT_MANAGE_CN_ORIGIN if _is_cn_host(host) else APPLE_ACCOUNT_MANAGE_ORIGIN


@dataclass
class AppleAccountState:
    """可持久化的 Apple Account 会话状态（对应 Go `AppleAccountState`，JSON 键一一对应）。

    随 `icloud_accounts.apple_session_enc` 加密存储；`cookies` 是唯一的凭据字段，
    不得进日志、不得进响应体。
    """

    cookies: dict[str, str] = field(default_factory=dict)
    scnt: str = ""
    api_key: str = ""
    session_id: str = ""
    host: str = ""
    origin: str = ""
    user_agent: str = APPLE_ACCOUNT_MANAGE_USER_AGENT
    saved_at: str = ""
    last_checked_at: str = ""
    manage_expires_at: str = ""
    last_check_ok: bool = False
    last_status: str = ""

    @classmethod
    def from_dict(cls, raw: object) -> AppleAccountState:
        data = raw if isinstance(raw, dict) else {}
        cookies = data.get("cookies")
        return cls(
            cookies={str(k): str(v) for k, v in cookies.items()} if isinstance(cookies, dict) else {},
            scnt=str(data.get("scnt") or ""),
            api_key=str(data.get("api_key") or ""),
            session_id=str(data.get("session_id") or ""),
            host=str(data.get("host") or ""),
            origin=str(data.get("origin") or ""),
            user_agent=str(data.get("user_agent") or APPLE_ACCOUNT_MANAGE_USER_AGENT),
            saved_at=str(data.get("saved_at") or ""),
            last_checked_at=str(data.get("last_checked_at") or ""),
            manage_expires_at=str(data.get("manage_expires_at") or ""),
            last_check_ok=bool(data.get("last_check_ok")),
            last_status=str(data.get("last_status") or ""),
        )

    def clone(self) -> AppleAccountState:
        return replace(self, cookies=dict(self.cookies))

    def to_dict(self) -> dict:
        return {
            "cookies": dict(self.cookies),
            "scnt": self.scnt,
            "api_key": self.api_key,
            "session_id": self.session_id,
            "host": self.host,
            "origin": self.origin,
            "user_agent": self.user_agent,
            "saved_at": self.saved_at,
            "last_checked_at": self.last_checked_at,
            "manage_expires_at": self.manage_expires_at,
            "last_check_ok": self.last_check_ok,
            "last_status": self.last_status,
        }

    def redacted(self) -> dict:
        """不含 Cookie / scnt / apiKey 的脱敏视图（只报状态字段）。"""
        data = self.to_dict()
        data.update(cookies={}, scnt="", api_key="", session_id="")
        data["has_cookies"] = bool(self.cookies)
        return data


def _parse_moment(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class AppleCreateResult:
    """建别名结果，与旧接口 `hme_client.CreateResult` 形状一致，便于上层统一处理。"""

    email: str
    label: str = ""
    created_at: str = ""


def response_snippet(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return "空响应"
    return text[:240] + "..." if len(text) > 240 else text


def parse_apple_retry_after(data: bytes) -> timedelta:
    try:
        parsed = json.loads(data.decode("utf-8", errors="replace"))
    except ValueError:
        return timedelta(0)
    return _parse_retry_after(parsed)


_AUTH_TOKENS = (
    "authentication_failed",
    "authentication failed",
    "auth_failed",
    "auth failed",
    "gsa_invalid_session",
    "invalid session",
    "scnt_expired",
    "session expired",
    "session has expired",
)


def body_looks_auth_expired(lower: str) -> bool:
    text = (lower or "").strip().lower()
    if not text:
        return False
    if any(token in text for token in _AUTH_TOKENS):
        return True
    return "authentication" in text and ("failed" in text or "expired" in text)


_CAPACITY_MARKERS = (
    "maximum number of address",
    "maximum address",
    "address capacity",
    "total address",
    "capacity reached",
    "hide my email address limit",
)


def classify_apple_error(stage: str, status: int, data: bytes) -> AliasError:
    """把 Apple Account 错误分类为可识别错误（对应 Go `appleError`）。

    优先级：总容量（文案最具体）→ 限速 → 会话失效 → 其余普通错误。
    """
    snippet = response_snippet(data)
    detail = f"{stage}失败: HTTP {status} ({snippet})"
    lower = snippet.lower()

    if any(marker in lower for marker in _CAPACITY_MARKERS):
        return AliasLimitReached(detail)

    retry_after = parse_apple_retry_after(data)
    if retry_after.total_seconds() > 0 or "limit" in lower or "too many" in lower or "rate" in lower:
        return AliasRateLimited(detail, retry_after=retry_after)

    if status == APPLE_ACCOUNT_HTTP_STATUS_SESSION_TIMEOUT or (
        status in (401, 403) and body_looks_auth_expired(lower)
    ):
        return AppleAccountAuthFailed(detail)
    return AliasError("APPLE_ACCOUNT_ERROR", detail)


_TRANSIENT_MARKERS = (
    "eof",
    "timeout",
    "timed out",
    "i/o timeout",
    "no such host",
    "temporary failure in name resolution",
    "network is unreachable",
    "no route to host",
    "connection reset",
    "connection refused",
    "connection aborted",
    "forcibly closed",
    "server closed idle connection",
    "tls handshake timeout",
)


def is_transient_network_error(error: BaseException) -> bool:
    """判断是否为可重试的瞬态网络错误（对应 Go `isAppleAccountTransientNetworkError`）。"""
    if isinstance(error, asyncio.CancelledError):
        return False
    if isinstance(error, httpx.TimeoutException | ConnectionError | httpx.RemoteProtocolError):
        return True
    text = str(error).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def js_log_body() -> list[dict]:
    """`/v2/jslogs` 心跳请求体（eventId 随机 UUID），对应 Go `appleAccountJSLogBody`。"""
    return [
        {
            "eventId": str(uuid.uuid4()),
            "timestamp": int(time.time() * 1000),
            "eventType": "custom",
            "componentName": "performance",
            "action": "memory",
            "metadata": {
                "domNodeCount": 0,
                "usedJSHeapSize": 0,
                "totalJSHeapSize": 0,
                "heapUtilization": 0,
                "createdAt": 0,
                "elapsedTime": 0,
                "marks": {"startTime": 0},
                "eventVersion": 1,
            },
        }
    ]


class AppleAccountClient:
    """Apple Account 管理接口客户端（新接口）。

    并发安全由调用方保证：Go 版用互斥锁串行化 refresh / create，Python 侧
    `alias_service` 按账号串行调用（单次请求生命周期内不共享实例）。
    """

    def __init__(
        self,
        state: AppleAccountState | dict | None = None,
        verbose: bool = False,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        transient_backoff: float = APPLE_ACCOUNT_TRANSIENT_BACKOFF,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.state = state.clone() if isinstance(state, AppleAccountState) else AppleAccountState.from_dict(state)
        if not self.state.origin:
            self.state.origin = manage_origin_for_host(self.state.host)
        if not self.state.user_agent:
            self.state.user_agent = APPLE_ACCOUNT_MANAGE_USER_AGENT
        self.verbose = verbose
        self._transport = transport
        self._backoff = transient_backoff
        self._client = client

    # ---- 会话状态 ----

    def current_state(self) -> AppleAccountState:
        return self.state.clone()

    def has_session(self) -> bool:
        """是否已保存会话（只看 Cookie）。"""
        return bool(self.state.cookies)

    def needs_refresh(self, now: datetime | None = None) -> bool:
        """scnt/apiKey 缺失、TTL 过期或上次检查失败时需要刷新。"""
        moment = now or datetime.now(UTC)
        if not self.state.scnt.strip() or not self.state.api_key.strip():
            return True
        checked = _parse_moment(self.state.last_checked_at)
        expires = _parse_moment(self.state.manage_expires_at)
        if checked is None or expires is None or not self.state.last_check_ok:
            return True
        return moment >= expires

    def needs_keep_alive(self, interval: timedelta | None = None, now: datetime | None = None) -> bool:
        """是否需要保活（对应 Go `NeedsKeepAlive`）。"""
        if not self.state.scnt.strip() or not self.state.api_key.strip():
            return False
        checked = _parse_moment(self.state.last_checked_at)
        if checked is None:
            return True
        if not self.state.last_check_ok:
            return True
        window = interval if interval and interval.total_seconds() > 0 else APPLE_ACCOUNT_KEEP_ALIVE_DEFAULT_INTERVAL
        return (now or datetime.now(UTC)) >= checked + window

    def _mark_ok(self) -> None:
        self.state.last_checked_at = datetime.now(UTC).isoformat()
        self.state.last_check_ok = True
        self.state.last_status = "新接口会话正常"

    def _mark_token_ttl(self, timeout_minutes: int, now: datetime | None = None) -> None:
        if timeout_minutes > 0:
            self.state.manage_expires_at = ((now or datetime.now(UTC)) + timedelta(minutes=timeout_minutes)).isoformat()

    def _mark_error(self, status: str) -> None:
        self.state.last_checked_at = datetime.now(UTC).isoformat()
        self.state.last_check_ok = False
        self.state.last_status = status

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"  [AppleAccount] {message}")

    # ---- Base URL 解析 ----

    def _manage_base(self) -> str:
        override = _base_url_override.strip().rstrip("/")
        if override and override != APPLE_ACCOUNT_MANAGE_BASE:
            return override
        return manage_base_for_host(self.state.host)

    def _portal_base(self) -> str:
        override = _base_url_override.strip().rstrip("/")
        if override and override != APPLE_ACCOUNT_MANAGE_BASE:
            return override
        if _is_cn_host(self.state.host):
            return APPLE_ACCOUNT_MANAGE_CN_ORIGIN
        return self.state.origin.rstrip("/") if self.state.origin.strip() else APPLE_ACCOUNT_MANAGE_ORIGIN

    def _origin(self) -> str:
        if _is_cn_host(self.state.host):
            return APPLE_ACCOUNT_MANAGE_CN_ORIGIN
        return self.state.origin.rstrip("/") if self.state.origin.strip() else APPLE_ACCOUNT_MANAGE_ORIGIN

    def _cookie_header(self) -> str:
        if not self.state.cookies:
            return ""
        parts = [
            f"{name}={self.state.cookies[name]}"
            for name in sorted(self.state.cookies)
            if name and self.state.cookies[name]
        ]
        return "; ".join(parts)

    # ---- 请求执行 ----

    def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=APPLE_ACCOUNT_REQUEST_TIMEOUT, transport=self._transport)

    async def _send(
        self, method: str, url: str, headers: dict[str, str], body: Any = None
    ) -> tuple[int, bytes, httpx.Headers]:
        own = self._client is None
        client = self._http()
        request_headers = {k: v for k, v in headers.items() if v}
        if cookie := self._cookie_header():
            request_headers["Cookie"] = cookie
        try:
            response = await client.request(
                method, url, headers=request_headers, json=body if body is not None else None
            )
        finally:
            if own:
                await client.aclose()
        for name, value in response.cookies.items():
            if name and value:
                self.state.cookies[name] = value
        self._update_state_from_headers(response.headers)
        return response.status_code, response.content, response.headers

    def _update_state_from_headers(self, headers: httpx.Headers) -> None:
        if value := headers.get("scnt", "").strip():
            self.state.scnt = value
        if value := headers.get("X-Apple-ID-Session-Id", "").strip():
            self.state.session_id = value

    async def _retry_transient(self, operation) -> Any:
        """瞬态网络错误重试 3 次，退避 800/1600/2400ms；其余错误立即返回。"""
        last: BaseException | None = None
        for attempt in range(APPLE_ACCOUNT_TRANSIENT_RETRIES):
            try:
                return await operation()
            except AliasError:
                raise
            except BaseException as error:
                if not is_transient_network_error(error):
                    raise
                last = error
                if attempt == APPLE_ACCOUNT_TRANSIENT_RETRIES - 1:
                    break
                if self._backoff > 0:
                    await asyncio.sleep(self._backoff * (attempt + 1))
        assert last is not None
        raise last

    # ---- 门户与 token ----

    def _portal_headers(self, accept: str, json_content: bool, dest: str, mode: str) -> dict[str, str]:
        base = self._portal_base()
        headers = {
            "Accept": accept,
            "Referer": base + "/",
            "User-Agent": self.state.user_agent,
            "Accept-Language": APPLE_ACCOUNT_MANAGE_LANGUAGE + ",en;q=0.9",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": mode,
            "Sec-Fetch-Dest": dest,
            "Sec-CH-UA-Platform": APPLE_ACCOUNT_MANAGE_PLATFORM,
            "Sec-CH-UA": _SEC_CH_UA,
            "Sec-CH-UA-Mobile": "?0",
        }
        if json_content:
            headers["Content-Type"] = "application/json"
            headers["X-Apple-I-Request-Context"] = APPLE_ACCOUNT_MANAGE_REQUEST_CTX
            headers["X-Apple-I-TimeZone"] = APPLE_ACCOUNT_MANAGE_TIME_ZONE
            headers["X-Apple-I-FD-Client-Info"] = fd_client_info(self.state.user_agent)
        return headers

    async def _portal_request_once(self, path: str, accept: str, json_content: bool, dest: str, mode: str) -> bytes:
        base = self._portal_base()
        status, data, _ = await self._send(
            "GET", base + path, self._portal_headers(accept, json_content, dest, mode)
        )
        if not 200 <= status < 300:
            raise classify_apple_error("打开隐私页面", status, data)
        return data

    async def _portal_request(self, path: str, accept: str, json_content: bool, dest: str, mode: str) -> bytes:
        return await self._retry_transient(
            lambda: self._portal_request_once(path, accept, json_content, dest, mode)
        )

    def _api_headers(self, with_api_key: bool = True) -> dict[str, str]:
        origin = self._origin()
        headers = {
            "Accept": _JSON_ACCEPT,
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin + "/",
            "User-Agent": self.state.user_agent,
            "Accept-Language": APPLE_ACCOUNT_MANAGE_LANGUAGE + ",en;q=0.9",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Sec-CH-UA-Platform": APPLE_ACCOUNT_MANAGE_PLATFORM,
            "Sec-CH-UA": _SEC_CH_UA,
            "Sec-CH-UA-Mobile": "?0",
            "X-Apple-I-FD-Client-Info": fd_client_info(self.state.user_agent),
            "X-Apple-I-Request-Context": APPLE_ACCOUNT_MANAGE_REQUEST_CTX,
            "X-Apple-I-TimeZone": APPLE_ACCOUNT_MANAGE_TIME_ZONE,
        }
        if scnt := self.state.scnt.strip():
            headers["scnt"] = scnt
        if with_api_key and (api_key := self.state.api_key.strip()):
            headers["X-Apple-Api-Key"] = api_key
        return headers

    async def _warm_portal(self) -> None:
        self._log("预热门户...")
        await self._portal_request(
            "/account/manage/section/privacy", _PRIVACY_ACCEPT, False, "document", "navigate"
        )
        data = await self._portal_request("/bootstrap/portal", _JSON_ACCEPT, True, "empty", "cors")
        try:
            portal = json.loads(data)
        except ValueError:
            return
        if isinstance(portal, dict):
            self._mark_token_ttl(int(portal.get("timeOutInterval") or 0))

    async def _fetch_token_scnt_once(self) -> None:
        url = self._manage_base() + "/account/manage/gs/ws/token"
        status, data, headers = await self._send("GET", url, self._api_headers(with_api_key=False))
        if scnt := headers.get("scnt", "").strip():
            self.state.scnt = scnt
        if not 200 <= status < 300:
            raise classify_apple_error("刷新管理 token", status, data)
        try:
            token = json.loads(data)
        except ValueError:
            return
        if isinstance(token, dict):
            self._mark_token_ttl(int(token.get("timeOutInterval") or 0))

    async def _fetch_token_scnt(self) -> None:
        self._log("获取管理 token (scnt)...")
        await self._retry_transient(self._fetch_token_scnt_once)

    async def _load_api_key_once(self) -> None:
        url = self._manage_base() + "/account/manage"
        status, data, _ = await self._send("GET", url, self._api_headers())
        if not 200 <= status < 300:
            raise classify_apple_error("读取管理入口", status, data)
        try:
            manage = json.loads(data)
        except ValueError:
            manage = None
        if isinstance(manage, dict) and (api_key := str(manage.get("apiKey") or "").strip()):
            self.state.api_key = api_key
        if not self.state.api_key.strip():
            raise AppleAccountAuthFailed("读取管理入口失败: Apple Account 管理接口未返回 api_key")

    async def _load_api_key(self) -> None:
        self._log("读取管理入口 (api_key)...")
        await self._retry_transient(self._load_api_key_once)

    async def _call_api_once(self, method: str, path: str, body: Any = None) -> tuple[int, bytes]:
        url = self._manage_base() + path
        status, data, _ = await self._send(method, url, self._api_headers(), body)
        return status, data

    async def _call_api(self, method: str, path: str, body: Any = None) -> tuple[int, bytes]:
        return await self._retry_transient(lambda: self._call_api_once(method, path, body))

    # ---- 会话引导 / 刷新 ----

    async def bootstrap(self) -> None:
        """从浏览器 Cookie 引导出完整会话（scnt + apiKey）。"""
        self._log("引导 Apple Account 管理会话...")
        if not self.state.cookies:
            raise AppleAccountAuthFailed("引导失败: 缺少 Apple Account Cookie")
        try:
            await self._warm_portal()
        except AliasError as error:
            self._mark_error(f"门户预热失败: {error.message or error.code}")
            raise
        try:
            await self._fetch_token_scnt()
        except AliasError as error:
            self._mark_error(f"获取管理 token 失败: {error.message or error.code}")
            raise
        try:
            await self._load_api_key()
        except AliasError as error:
            self._mark_error(f"获取管理 api_key 失败: {error.message or error.code}")
            raise
        self._mark_ok()
        self._log("会话就绪 (scnt/apiKey 已获取)")

    async def refresh(self) -> None:
        """刷新会话（scnt + apiKey）；无 scnt 时退化为完整引导。"""
        self._log("刷新 Apple Account 管理会话...")
        if not self.state.scnt.strip():
            await self._bootstrap_locked()
            return
        previous = self.state.scnt
        try:
            await self._fetch_token_scnt()
        except AliasError as error:
            self.state.scnt = ""
            await self._safe_warm_portal()
            try:
                await self._fetch_token_scnt()
            except AliasError as retry_error:
                self.state.scnt = previous
                self._mark_error(f"刷新管理 token 失败: {error.message or error.code}")
                raise retry_error from error
        try:
            await self._load_api_key()
        except AliasError as error:
            self.state.scnt = ""
            await self._safe_warm_portal()
            try:
                await self._fetch_token_scnt()
            except AliasError as retry_error:
                self.state.scnt = previous
                self._mark_error(f"刷新会话失败: {error.message or error.code}")
                raise retry_error from error
            try:
                await self._load_api_key()
            except AliasError as retry_error:
                self.state.scnt = previous
                self._mark_error(f"获取管理 api_key 失败: {retry_error.message or retry_error.code}")
                raise
        self._mark_ok()

    async def _safe_warm_portal(self) -> None:
        try:
            await self._warm_portal()
        except AliasError:
            pass

    async def _bootstrap_locked(self) -> None:
        try:
            await self._warm_portal()
        except AliasError as error:
            self._mark_error(f"门户预热失败: {error.message or error.code}")
            raise
        try:
            await self._fetch_token_scnt()
        except AliasError as error:
            self._mark_error(f"获取管理 token 失败: {error.message or error.code}")
            raise
        try:
            await self._load_api_key()
        except AliasError as error:
            self._mark_error(f"获取管理 api_key 失败: {error.message or error.code}")
            raise
        self._mark_ok()

    # ---- 业务调用 ----

    async def create_alias(self, label: str = "") -> AppleCreateResult:
        """通过新接口创建一个隐藏邮箱（add → complete → confirm 三步）。"""
        if self.needs_refresh():
            self._log("会话需要刷新, 先刷新...")
            try:
                if not self.state.scnt.strip():
                    await self._bootstrap_locked()
                else:
                    await self.refresh()
            except AliasError as error:
                raise AppleAccountAuthFailed(f"会话刷新失败: {error.message or error.code}") from error

        label = label.strip() or f"Created {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}"
        note = "Created by icloud_hme tool"

        try:
            result = await self._create_alias_locked(label, note)
        except AppleAccountAuthFailed as error:
            # 创建中途会话失效（scnt 轮换/过期）→ 刷新后重试一次
            self._log("创建时会话失效, 刷新后重试...")
            try:
                await self.refresh()
            except AliasError:
                raise error from None
            return await self._create_alias_locked(label, note)
        return result

    async def _create_alias_locked(self, label: str, note: str) -> AppleCreateResult:
        # 1. 生成候选地址
        stage = "生成候选隐私邮箱"
        status, raw = await self._call_api("POST", "/account/manage/email/private/add", {})
        if not 200 <= status < 300:
            raise classify_apple_error(stage, status, raw)
        try:
            generated = json.loads(raw)
        except ValueError:
            generated = None
        email = ""
        if isinstance(generated, dict):
            email = str(generated.get("emailAddress") or "").strip()
        if not email:
            raise AliasError("APPLE_ACCOUNT_ERROR", f"{stage}失败: Apple Account 未返回候选隐私邮箱 ({response_snippet(raw)})")

        # 2. 确认创建
        stage = "确认创建隐私邮箱"
        complete_body = {"emailAddress": email, "label": label, "note": note}
        status, raw = await self._call_api("PUT", "/account/manage/email/private/add/complete", complete_body)
        if not 200 <= status < 300:
            raise classify_apple_error(stage, status, raw)
        try:
            completed = json.loads(raw)
        except ValueError:
            raise AliasError(
                "APPLE_ACCOUNT_ERROR", f"{stage}失败: Apple Account 返回无法解析 ({response_snippet(raw)})"
            ) from None
        if not isinstance(completed, dict):
            raise AliasError(
                "APPLE_ACCOUNT_ERROR", f"{stage}失败: Apple Account 返回无法解析 ({response_snippet(raw)})"
            )

        created_email = str(completed.get("emailAddress") or email or "").strip().lower()
        result = AppleCreateResult(
            email=created_email,
            label=str(completed.get("label") or label),
            created_at=datetime.now(UTC).isoformat(),
        )

        # 3. 确认详情（尽力而为，失败不影响创建结果）
        completed_id = str(completed.get("id") or "").strip()
        if completed_id:
            status, raw = await self._call_api(
                "GET", f"/account/manage/email/private/{quote(completed_id, safe='')}.em"
            )
            if 200 <= status < 300:
                try:
                    confirmed = json.loads(raw)
                except ValueError:
                    confirmed = None
                if isinstance(confirmed, dict):
                    if value := str(confirmed.get("emailAddress") or "").strip():
                        result = replace(result, email=value.lower())
                    if value := str(confirmed.get("label") or "").strip():
                        result = replace(result, label=value)

        if not result.email:
            raise AliasError("APPLE_ACCOUNT_ERROR", "确认创建隐私邮箱失败: Apple Account 创建后未返回隐私邮箱")
        self._mark_ok()
        self._log(f"已通过新接口创建: {result.email}")
        return result

    async def keep_alive(self) -> None:
        """保活会话：刷新 → GET forwardemail（失败即报错）→ best-effort POST /v2/jslogs。"""
        self._log("保活 Apple Account 管理会话...")
        await self.refresh()
        status, data = await self._call_api("GET", APPLE_ACCOUNT_KEEP_ALIVE_FORWARD_EMAIL_PATH)
        if not 200 <= status < 300:
            raise classify_apple_error("保活检查转发邮箱", status, data)
        try:
            await self._call_api("POST", APPLE_ACCOUNT_KEEP_ALIVE_JS_LOGS_PATH, js_log_body())
        except (AliasError, httpx.HTTPError) as error:
            if os.environ.get("ICLOUD_DEBUG_APPLE_ACCOUNT") == "1":
                print(f"[icloud-api] keepalive jslogs ignored err={error!r}", flush=True)
        self._mark_ok()
