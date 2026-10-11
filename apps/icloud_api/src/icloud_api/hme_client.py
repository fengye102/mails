"""iCloud Hide My Email 上游客户端（对应 CYMail `internal/hme/client.go`）。

协议细节、端点路径、查询参数与错误码全部按原 Go 实现照搬：
Cookie 走 iCloud Web 会话，`X-APPLE-WEBAUTH-*` 同时作为请求头镜像；
`buildURL` 会补 clientBuildNumber / clientMasteringNumber / clientId / dsid；
maildomainws 用 build 2624Build22 且 Content-Type 为 text/plain，其余端点用 2624Build13。

与原实现一致的行为，改动前请先看 Go 源码：
- 401/403 表示会话失效，不重试直接失败；
- 421 表示 Apple 要求刷新网页信任状态，能用响应里的 trustTokens 换到新 token 才重试；
- 建别名是「generate → reserve」两步，重试时会清掉 serviceURL 强制重新校验会话。
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from icloud_api.aliases import (
    CLIENT_BUILD_NUMBER,
    CLIENT_MASTERING_NUMBER,
    DEFAULT_BUILD_NUMBER,
    MAX_RETRIES,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_DELAYS,
    AliasError,
    ForwardingSettings,
    TrustSessionRequired,
    operation_error,
    parse_forwarding_settings,
)

APPLE_SESSION_HEADER_PREFIX = "X-APPLE-WEBAUTH-"
APPLE_SESSION_HEADERS = frozenset(
    {"X-APPLE-DS-WEB-SESSION-TOKEN", "X-APPLE-UNIQUE-CLIENT-ID", "X-APPLE-GROUP"}
)
TRUST_COOKIE_NAME = "X-APPLE-WEBAUTH-HSA-TRUST"
TRUST_TOKEN_CHARS = frozenset("+/_=-")
MAX_ERROR_MESSAGE_LENGTH = 200
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class UpstreamHTTPError(AliasError):
    """带上游状态码的错误。调用方按 `status_code` 判类别，别去匹配错误文本。"""

    def __init__(self, status_code: int, message: str = "") -> None:
        self.status_code = status_code
        self.raw_message = message
        text = message
        if not text:
            try:
                text = HTTPStatus(status_code).phrase
            except ValueError:
                text = ""
        super().__init__("UPSTREAM_HTTP_ERROR", f"HTTP {status_code}: {text}".rstrip(": "))


@dataclass
class AccountInfo:
    dsid: str = ""
    apple_id: str = ""
    primary_email: str = ""
    full_name: str = ""
    is_managed_apple_id: bool = False


@dataclass
class CreateResult:
    email: str
    label: str
    created_at: str


@dataclass
class HmeClient:
    """一个实例对应一个 iCloud 账号的 Web 会话。"""

    cookies: dict[str, str] = field(default_factory=dict)
    host: str = "icloud.com"
    proxy: str = ""
    verbose: bool = False
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    transport: httpx.AsyncBaseTransport | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    logger: logging.Logger | None = None
    account_info: AccountInfo | None = None
    service_url: str = ""
    setup_url: str = ""
    dsid: str = ""
    client_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    _http: httpx.AsyncClient | None = None
    _owns_http: bool = False

    def __post_init__(self) -> None:
        self.host = normalize_host(self.host)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
        self._http = None
        self._owns_http = False

    async def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
                proxy=self.proxy or None,
            )
            # Cookie 由我们自己按原实现拼请求头，不让 httpx 再塞一份。
            self._owns_http = self.transport is None
        return self._http

    def log(self, message: str) -> None:
        if self.verbose and self.logger is not None:
            self.logger.info("[iCloud] %s", message)

    # ---- 会话 ----

    async def validate_session(self) -> AccountInfo:
        """校验会话并解析 HME 服务端点。业务方法前必须先成功一次。"""
        self.log("校验 iCloud 会话...")
        body = await self.request(
            "POST", f"{self.setup_endpoint()}/validate", None, timeout=20.0, max_attempts=MAX_RETRIES
        )
        data = _loads_object(body)
        service_url = _dig(data, "webservices", "premiummailsettings", "url")
        if not service_url:
            raise AliasError(
                "SESSION_INVALID",
                "iCloud 会话校验失败 — 可能原因：未开通 iCloud+ 订阅；Cookie 已过期需重新登录 icloud.com；网络问题",
            )
        self.service_url = service_url.rstrip("/")
        # 剥掉 :443——带端口的 host 存不住 cookie，会导致后续请求 401。
        self.service_url = self.service_url.removesuffix(":443")

        ds_info = data.get("dsInfo") if isinstance(data.get("dsInfo"), dict) else {}
        self.dsid = _text(ds_info.get("dsid"))
        info = AccountInfo(
            dsid=self.dsid,
            apple_id=_first(
                ds_info.get("appleId"), ds_info.get("primaryEmail"), ds_info.get("appleIdEmail")
            ),
            primary_email=_first(ds_info.get("primaryEmail"), ds_info.get("appleId")),
            full_name=_first(ds_info.get("fullName"), ds_info.get("name")),
            is_managed_apple_id=bool(ds_info.get("isManagedAppleId")),
        )
        if not info.apple_id:
            for name in ("aosappleid", "appleId", "dsid"):
                if self.cookies.get(name):
                    info.apple_id = self.cookies[name]
                    break
        self.account_info = info
        self.log(f"会话有效 → {info.apple_id or '未知账号'}")
        return info

    async def _resolve_service(self) -> None:
        if not self.service_url:
            await self.validate_session()

    def setup_endpoint(self) -> str:
        if not self.setup_url:
            suffix = "setup.icloud.com.cn" if self.host == "icloud.com.cn" else "setup.icloud.com"
            self.setup_url = f"https://{suffix}/setup/ws/1"
        return self.setup_url

    def origin(self) -> str:
        return f"https://www.{self.host}"

    # ---- 别名 ----

    async def list_forwarding_settings(self) -> ForwardingSettings:
        await self._resolve_service()
        self.log("获取别名列表...")
        body = await self.request(
            "GET", f"{self.service_url}/v2/hme/list", None, max_attempts=MAX_RETRIES
        )
        return parse_forwarding_settings(body)

    async def list_aliases(self):
        return (await self.list_forwarding_settings()).aliases

    async def update_forward_to(self, email: str) -> None:
        await self._resolve_service()
        target = email.strip().lower()
        if not target or "@" not in target:
            raise AliasError("INVALID_FORWARD_TO", "invalid forwarding email")
        body = await self.request(
            "POST",
            f"{self.service_url}/v1/hme/updateForwardTo",
            {"forwardToEmail": target},
            max_attempts=2,
        )
        if not _success(body):
            raise operation_error("updateForwardTo", body)

    async def generate(self) -> str:
        await self._resolve_service()
        self.log("生成候选别名...")
        body = await self.request(
            "POST", f"{self.service_url}/v1/hme/generate", {"langCode": "en-us"}, max_attempts=2
        )
        data = _loads_object(body)
        if not _success(body):
            raise operation_error("generate", body)
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        hme = result.get("hme")
        if isinstance(hme, str):
            return hme
        # 有些响应把 hme 包在嵌套对象里。
        nested = hme if isinstance(hme, dict) else {}
        return _first(nested.get("hme"), nested.get("email"))

    async def reserve(self, hme: str, label: str = "") -> str:
        await self._resolve_service()
        if not label:
            label = "Created " + datetime.now().strftime("%Y-%m-%d %H:%M")
        self.log(f"保留别名 {hme} ...")
        body = await self.request(
            "POST",
            f"{self.service_url}/v1/hme/reserve",
            {"hme": hme, "label": label, "note": "Created by icloud_hme tool"},
            max_attempts=2,
        )
        data = _loads_object(body)
        if not _success(body):
            raise operation_error("reserve", body)
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        reserved = result.get("hme")
        if isinstance(reserved, dict) and _text(reserved.get("hme")):
            return _text(reserved.get("hme"))
        return hme

    async def create_alias(self, label: str = "", max_retries: int = 5) -> CreateResult:
        """一步建别名（内部 generate + reserve）。重试会清掉 serviceURL 强制重新校验。"""
        attempts = max_retries if max_retries > 0 else 5
        last_error: BaseException | None = None
        for attempt in range(attempts):
            if attempt > 0:
                self.service_url = ""
                self.setup_url = ""
                self.log(f"重试 {attempt + 1}/{attempts} ...")
            try:
                hme = await self.generate()
                email = await self.reserve(hme, label)
            except AliasError as error:
                last_error = error
                self.log(str(error))
                # 容量用尽与限速都别重试，重试只会加深封禁。
                if error.code in {"ALIAS_LIMIT_REACHED", "ALIAS_RATE_LIMITED"}:
                    break
                if attempt < attempts - 1:
                    await self.sleep(1)
                    continue
                break
            return CreateResult(
                email=email, label=label, created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        if last_error is not None:
            raise AliasError("ALIAS_CREATE_FAILED", f"创建别名失败: {last_error}") from last_error
        raise AliasError("ALIAS_CREATE_FAILED", f"创建别名失败，已重试 {attempts} 次")

    async def deactivate(self, anonymous_id: str) -> bool:
        return await self._set_alias_state("deactivate", anonymous_id)

    async def reactivate(self, anonymous_id: str) -> bool:
        return await self._set_alias_state("reactivate", anonymous_id)

    async def _set_alias_state(self, operation: str, anonymous_id: str) -> bool:
        await self._resolve_service()
        self.log(f"{operation} {anonymous_id} ...")
        body = await self.request(
            "POST",
            f"{self.service_url}/v1/hme/{operation}",
            {"anonymousId": anonymous_id},
            max_attempts=2,
        )
        if not _success(body):
            raise operation_error(operation, body)
        return True

    async def delete(self, anonymous_id: str) -> None:
        """删除别名。直接删不掉时先停用再删（Apple 对活跃别名会拒绝直删）。"""
        await self._resolve_service()
        self.log(f"删除 {anonymous_id} ...")
        payload = {"anonymousId": anonymous_id}
        body = ""
        try:
            body = await self.request(
                "POST", f"{self.service_url}/v1/hme/delete", payload, max_attempts=2
            )
        except AliasError:
            body = ""
        if not _success(body):
            self.log("直接删除失败，尝试先停用...")
            deactivate_body = await self.request(
                "POST", f"{self.service_url}/v1/hme/deactivate", payload, max_attempts=2
            )
            if not _success(deactivate_body):
                raise operation_error("deactivate before delete", deactivate_body)
            body = await self.request(
                "POST", f"{self.service_url}/v1/hme/delete", payload, max_attempts=2
            )
            if not _success(body):
                raise operation_error("delete", body)
        self.log("已删除")

    # ---- 传输层 ----

    def build_url(self, raw_url: str) -> str:
        """补 iCloud Web API 强制的查询参数。"""
        parts = urlsplit(raw_url)
        query = dict(_parse_query(parts.query))
        host = parts.hostname or ""
        if "maildomainws" in host:
            query["clientBuildNumber"] = CLIENT_BUILD_NUMBER
            query["clientMasteringNumber"] = CLIENT_MASTERING_NUMBER
        else:
            query["clientBuildNumber"] = DEFAULT_BUILD_NUMBER
            query["clientMasteringNumber"] = DEFAULT_BUILD_NUMBER
        if self.client_id:
            query["clientId"] = self.client_id
        if self.dsid:
            query["dsid"] = self.dsid
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def request_headers(self, hostname: str) -> dict[str, str]:
        is_maildomainws = "maildomainws" in hostname
        headers = {
            "Origin": self.origin(),
            "Referer": self.origin() + "/",
            "Accept": "*/*" if is_maildomainws else "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Connection": "keep-alive",
            "Content-Type": "text/plain" if is_maildomainws else "application/json",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if self.cookies:
            # iCloud 把 WebAuth 值当 cookie 存，但 setup/HME 端点同时要求同名请求头。
            for name, value in self.cookies.items():
                if is_apple_session_header(name):
                    headers[name] = trim_outer_quotes(value)
            # 浏览器发的 Cookie 值带双引号，iCloud 严格匹配，所以没引号也要补上。
            headers["Cookie"] = "; ".join(
                f"{name}={value}" if value.startswith('"') else f'{name}="{value}"'
                for name, value in self.cookies.items()
            )
        return headers

    async def request(
        self,
        method: str,
        raw_url: str,
        body: object | None = None,
        timeout: float | None = None,
        max_attempts: int = MAX_RETRIES,
    ) -> str:
        attempts = max_attempts or MAX_RETRIES
        full_url = self.build_url(raw_url)
        hostname = urlsplit(raw_url).hostname or ""
        payload = json.dumps(body).encode() if body is not None else None
        client = await self.http()
        last_error: BaseException | None = None

        for attempt in range(1, attempts + 1):
            headers = self.request_headers(hostname)
            try:
                response = await client.request(
                    method,
                    full_url,
                    content=payload,
                    headers=headers,
                    timeout=timeout or self.timeout_seconds,
                )
            except httpx.HTTPError as error:
                last_error = AliasError("UPSTREAM_UNREACHABLE", f"连接失败: {error}")
                if attempt < attempts:
                    await self._sleep_retry(attempt)
                    continue
                raise last_error from error

            text = response.text
            self._absorb_cookies(response)

            if 200 <= response.status_code < 300:
                return text

            if response.status_code == HTTPStatus.MISDIRECTED_REQUEST:
                updated = self._apply_trust_challenge(text)
                last_error = TrustSessionRequired("Apple 要求刷新网页信任状态")
                if updated and attempt < attempts:
                    continue
                raise last_error

            last_error = safe_apple_http_error(response.status_code, text)
            # 401/403 说明 Cookie 失效，重试没意义。
            if response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                raise last_error
            if attempt < attempts:
                await self._sleep_retry(attempt)
                continue
            raise last_error

        raise last_error or AliasError("UPSTREAM_ERROR", "未知错误")

    async def _sleep_retry(self, attempt: int) -> None:
        index = min(attempt - 1, len(RETRY_DELAYS) - 1)
        await self.sleep(RETRY_DELAYS[index].total_seconds())

    def _absorb_cookies(self, response: httpx.Response) -> None:
        """按原实现模拟浏览器行为：上游刷新的 token 要回写到会话里。"""
        for cookie in response.cookies.jar:
            if cookie.name and cookie.value:
                self.cookies[cookie.name] = cookie.value

    def _apply_trust_challenge(self, body: str) -> bool:
        data = _loads_object(body)
        tokens = data.get("trustTokens")
        if not isinstance(tokens, list):
            return False
        current = trim_outer_quotes(self.cookies.get(TRUST_COOKIE_NAME, ""))
        for item in tokens:
            token = _text(item)
            if not valid_trust_token(token) or token == current:
                continue
            self.cookies[TRUST_COOKIE_NAME] = token
            return True
        return False


def normalize_host(host: str) -> str:
    raw = (host or "").strip().lower()
    if "://" in raw:
        raw = urlsplit(raw).hostname or raw
    else:
        raw = urlsplit("https://" + raw).hostname or raw
    if raw == "icloud.com.cn" or raw.endswith(".icloud.com.cn"):
        return "icloud.com.cn"
    return "icloud.com"


def is_apple_session_header(name: str) -> bool:
    upper = (name or "").strip().upper()
    return upper.startswith(APPLE_SESSION_HEADER_PREFIX) or upper in APPLE_SESSION_HEADERS


def trim_outer_quotes(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    return text


def valid_trust_token(token: str) -> bool:
    if len(token) < 32 or len(token) > 8192:
        return False
    return all(char.isalnum() or char in TRUST_TOKEN_CHARS for char in token)


def safe_apple_http_error(status: int, body: str) -> UpstreamHTTPError:
    data = _loads_object(body)
    message = _first(
        _dig(data, "error", "errorMessage"),
        _dig(data, "error", "message"),
        _dig(data, "error", "reason"),
        data.get("message") if isinstance(data.get("message"), str) else "",
    )
    if not message:
        try:
            message = HTTPStatus(status).phrase
        except ValueError:
            message = ""
    message = message.replace("\r", " ").replace("\n", " ")[:MAX_ERROR_MESSAGE_LENGTH]
    return UpstreamHTTPError(status, message)


def _parse_query(query: str) -> list[tuple[str, str]]:
    return parse_qsl(query, keep_blank_values=True)


def _loads_object(body: str) -> dict:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _success(body: str) -> bool:
    return bool(_loads_object(body).get("success"))


def _dig(value: object, *keys: str) -> str:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first(*values: object) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""
