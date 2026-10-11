"""OutlookEmailPlus（OEP 注册邮箱池）来源管理适配器。

只负责「管理动作」：探测、列出/新增/删除账号、刷新令牌、导出账号清单。
复用连接里已加密保存的 OEP 凭据，不参与收件箱同步，也不写任何持久数据。

上游契约（以 outlook-email-plus v2.9.10 容器内 ``/app/outlook_web`` 源码为准）：

- 账号管理接口 ``/api/accounts*`` 与 ``/api/pool-admin/*`` 全部是「登录会话」鉴权
  （Flask session cookie），**不接受** ``X-API-Key``；对外机器接口 ``/api/external/*``
  只覆盖读取与号池领取，不含账号增删改。
- 因此本适配器的 ``secret`` 是 **OEP 登录口令**：先 ``POST /login {"password": secret}``
  换取会话 Cookie，再用该 Cookie 调用 ``/api/accounts*``。
- 写操作（``POST``/``DELETE``）另需会话级 ``X-CSRFToken``，取值来自 ``GET /api/csrf-token``
  → ``{"csrf_token": ...}``（该响应**没有** ``success`` 字段）；缺了真实 OEP 会返回
  ``400 CSRF_TOKEN_INVALID``。只读请求不需要。
- 列账号分页：``page_size`` 上限 100，**页码越界会被上游夹回最后一页**（不是返回空页），
  所以结束条件必须用 ``pagination.total_count``；靠「返回不足一页就结束」会无限翻页，
  账号数恰好是页大小整数倍时必然踩中。
- 所有失败一律映射为 ``base.SOURCE_ERROR_CODES`` 里的固定错误码；登录口令、会话 Cookie、
  上游 message 原文都不得进入异常消息、日志或响应体。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from mails_api.sources_admin.base import SourceAdminError, SourceItem

_TIMEOUT_SECONDS = 20.0
_PAGE_SIZE = 100
_MAX_PAGES = 100
_UNSAFE_KEY_CHARS = "/?#\\"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def build(client: httpx.AsyncClient) -> OutlookPlusSourceAdmin:
    """``sources_admin.admin_for`` 依赖的工厂。"""
    return OutlookPlusSourceAdmin(client)


def _contract_error() -> SourceAdminError:
    return SourceAdminError("SOURCE_CONTRACT")


def _text(value: Any, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise _contract_error()
    if required and not value.strip():
        raise _contract_error()
    if "\x00" in value:
        raise _contract_error()
    return value


def _optional_text(value: Any) -> str:
    return "" if value is None else _text(value)


def _key_text(value: Any) -> str:
    """账号标识（邮箱）路径参数；拒绝路径穿越、空白与控制字符。"""
    text = _text(value, required=True).strip()
    if len(text) > 320 or any(ch in text for ch in _UNSAFE_KEY_CHARS):
        raise _contract_error()
    if any(ch.isspace() or ord(ch) < 32 for ch in text):
        raise _contract_error()
    return text


def _pagination_total(payload: dict[str, Any], fallback: int | None) -> int | None:
    """读取 ``pagination.total_count``；缺失或形状不对时沿用上一次的值（None 表示上游没给）。"""
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        return fallback
    value = pagination.get("total_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback
    return value


def _iso_utc(value: Any) -> str | None:
    """把上游时间规范化为 UTC ISO8601（Z 结尾）；缺失返回 None。

    OEP 用 SQLite ``CURRENT_TIMESTAMP`` 存 ``"YYYY-MM-DD HH:MM:SS"``（无时区），
    按 UTC 解释；也兼容 ``...Z`` 形式。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise _contract_error()
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _contract_error() from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise _contract_error() from None


class OutlookPlusSourceAdmin:
    """OEP 管理适配器；``secret`` 是 OEP 登录口令，用会话 Cookie 访问账号接口。"""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    # ---- 传输层 ----

    async def _login(self, base_url: str, secret: str) -> httpx.Cookies:
        """用登录口令换会话 Cookie；失败按固定错误码映射。"""
        try:
            response = await self.client.post(
                base_url.rstrip("/") + "/login",
                json={"password": secret},
                timeout=httpx.Timeout(_TIMEOUT_SECONDS, connect=5),
                follow_redirects=False,
            )
        except (httpx.TimeoutException, TimeoutError):
            raise SourceAdminError("SOURCE_TIMEOUT") from None
        except httpx.RequestError:
            raise SourceAdminError("SOURCE_UNAVAILABLE") from None
        status = response.status_code
        if status in (401, 403):
            raise SourceAdminError("SOURCE_AUTH")
        if 500 <= status <= 599:
            raise SourceAdminError("SOURCE_UNAVAILABLE")
        if status == 429:
            raise SourceAdminError("SOURCE_REJECTED")
        if not 200 <= status <= 299:
            raise SourceAdminError("SOURCE_REJECTED")
        try:
            payload = response.json()
        except ValueError:
            raise _contract_error() from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise _contract_error()
        if not response.cookies:
            raise _contract_error()
        return response.cookies

    @staticmethod
    def _cookie_header(cookies: httpx.Cookies) -> str:
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookies.jar)

    async def _csrf_token(self, url: str, cookies: httpx.Cookies) -> str:
        """取当前会话的 CSRF token（写操作必需；响应没有 ``success`` 字段）。

        上游会顺手轮换会话 Cookie（token 存在服务端会话里），所以必须把响应 Cookie 合并回
        ``cookies``，否则后续写请求会报 ``CSRF_TOKEN_INVALID: The CSRF session token is missing.``
        """
        target = httpx.URL(url).copy_with(path="/api/csrf-token", query=None)
        response = await self._do("GET", str(target), cookies=cookies)
        try:
            payload = response.json()
        except ValueError:
            raise _contract_error() from None
        token = payload.get("csrf_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise _contract_error()
        if response.cookies:
            cookies.update(response.cookies)
        return token

    async def _do(
        self,
        method: str,
        url: str,
        *,
        cookies: httpx.Cookies | None = None,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        if cookies is not None:
            if method.upper() in _MUTATING_METHODS and "X-CSRFToken" not in request_headers:
                # 真实 OEP 的写操作要求会话级 CSRF token，缺了会 400 CSRF_TOKEN_INVALID。
                # 取 token 会同时轮换会话 Cookie，所以必须在构造 Cookie 头之前完成。
                request_headers["X-CSRFToken"] = await self._csrf_token(url, cookies)
            # 显式构造 Cookie 头，避免污染共享 client 的 cookie jar。
            request_headers["Cookie"] = self._cookie_header(cookies)
        try:
            response = await self.client.request(
                method,
                url,
                headers=request_headers,
                params=params,
                json=json,
                timeout=httpx.Timeout(_TIMEOUT_SECONDS, connect=5),
                follow_redirects=False,
            )
        except (httpx.TimeoutException, TimeoutError):
            raise SourceAdminError("SOURCE_TIMEOUT") from None
        except httpx.RequestError:
            raise SourceAdminError("SOURCE_UNAVAILABLE") from None
        status = response.status_code
        if status in (401, 403):
            raise SourceAdminError("SOURCE_AUTH")
        if status == 404:
            raise SourceAdminError("SOURCE_NOT_FOUND")
        if status == 409:
            raise SourceAdminError("SOURCE_CONFLICT")
        if 500 <= status <= 599:
            raise SourceAdminError("SOURCE_UNAVAILABLE")
        if not 200 <= status <= 299:
            # 400/422/429 等上游明确拒绝；不含上游 message，避免敏感内容外泄。
            raise SourceAdminError("SOURCE_REJECTED")
        return response

    async def _request(
        self,
        method: str,
        base_url: str,
        secret: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        cookies = await self._login(base_url, secret)
        return await self._do(
            method,
            base_url.rstrip("/") + path,
            cookies=cookies,
            params=params,
            json=json,
            headers=headers,
        )

    @staticmethod
    def _envelope(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            raise _contract_error() from None
        if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool):
            raise _contract_error()
        return payload

    async def _json_request(self, method: str, base_url: str, secret: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._request(method, base_url, secret, path, **kwargs)
        return self._envelope(response)

    # ---- 数据映射 ----

    @staticmethod
    def _item(account: Any) -> SourceItem:
        if not isinstance(account, dict):
            raise _contract_error()
        address = _text(account.get("email"), required=True)
        return SourceItem(
            provider_key=address,
            address=address,
            status=_optional_text(account.get("status")) or "unknown",
            created_at=_iso_utc(account.get("created_at")),
            detail={
                "id": account.get("id"),
                "account_type": _optional_text(account.get("account_type")),
                "provider": _optional_text(account.get("provider")),
                "group_name": _optional_text(account.get("group_name")),
                "remark": _optional_text(account.get("remark")),
                "last_refresh_at": _iso_utc(account.get("last_refresh_at")),
            },
        )

    # ---- 协议三件套 ----

    async def probe(self, base_url: str, secret: str, config: dict | None = None) -> None:
        payload = await self._json_request(
            "GET", base_url, secret, "/api/accounts", params={"page": "1", "page_size": "1"}
        )
        if payload.get("success") is not True or not isinstance(payload.get("accounts"), list):
            raise _contract_error()

    async def list_items(
        self, base_url: str, secret: str, config: dict | None = None
    ) -> list[SourceItem]:
        items: list[SourceItem] = []
        seen: set[str] = set()
        page = 1
        total_count: int | None = None
        while True:
            payload = await self._json_request(
                "GET",
                base_url,
                secret,
                "/api/accounts",
                params={"page": str(page), "page_size": str(_PAGE_SIZE)},
            )
            if payload.get("success") is not True:
                raise SourceAdminError("SOURCE_REJECTED")
            accounts = payload.get("accounts")
            if not isinstance(accounts, list) or len(accounts) > _PAGE_SIZE:
                raise _contract_error()
            total_count = _pagination_total(payload, total_count)
            added = 0
            for account in accounts:
                item = self._item(account)
                if item.provider_key in seen:
                    # 上游把越界页码夹回时会重复投递同一批账号，去重避免列表里出现两份。
                    continue
                seen.add(item.provider_key)
                items.append(item)
                added += 1
            if not accounts or added == 0:
                # 空页，或这一页没有任何新账号（上游无法继续前进）——都不能再翻。
                break
            if total_count is None:
                # 上游没给总数时，退回「短页即结束」的保守判断。
                if len(accounts) < _PAGE_SIZE:
                    break
            elif len(items) >= total_count:
                break
            page += 1
            if page > _MAX_PAGES:
                raise _contract_error()
        return items

    async def create_item(
        self,
        base_url: str,
        secret: str,
        payload: dict[str, Any],
        config: dict | None = None,
    ) -> SourceItem:
        if not isinstance(payload, dict):
            raise _contract_error()
        account_string = payload.get("account_string")
        if account_string is None:
            email = _text(payload.get("email"), required=True)
            account_string = "----".join(
                [
                    email,
                    _optional_text(payload.get("password")),
                    _optional_text(payload.get("client_id")),
                    _optional_text(payload.get("refresh_token")),
                ]
            )
        else:
            account_string = _text(account_string, required=True)
        address = account_string.split("----", 1)[0].strip()
        if "@" not in address or len(address) > 320 or any(ch.isspace() for ch in address):
            raise _contract_error()
        group_id = payload.get("group_id", 1)
        if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id < 0:
            raise _contract_error()
        provider = _text(payload.get("provider", "outlook"), required=True)
        add_to_pool = payload.get("add_to_pool", False)
        if not isinstance(add_to_pool, bool):
            raise _contract_error()
        data = await self._json_request(
            "POST",
            base_url,
            secret,
            "/api/accounts",
            json={
                "account_string": account_string,
                "group_id": group_id,
                "provider": provider,
                "add_to_pool": add_to_pool,
            },
        )
        if data.get("success") is not True:
            raise SourceAdminError("SOURCE_REJECTED")
        summary = data.get("summary")
        if not isinstance(summary, dict):
            raise _contract_error()
        imported = summary.get("imported")
        if isinstance(imported, bool) or not isinstance(imported, int):
            raise _contract_error()
        if imported < 1:
            raise SourceAdminError("SOURCE_REJECTED")
        return SourceItem(
            provider_key=address,
            address=address,
            status="active",
            created_at=None,
            detail={"group_id": group_id, "provider": provider, "imported": imported},
        )

    async def delete_item(
        self,
        base_url: str,
        secret: str,
        provider_key: str,
        config: dict | None = None,
    ) -> None:
        key = _key_text(provider_key)
        data = await self._json_request(
            "DELETE",
            base_url,
            secret,
            f"/api/accounts/email/{quote(key, safe='@')}",
        )
        if data.get("success") is not True:
            raise SourceAdminError("SOURCE_REJECTED")

    # ---- 来源专有动作 ----

    async def refresh_token(
        self,
        base_url: str,
        secret: str,
        account_id: Any,
        config: dict | None = None,
    ) -> dict[str, Any]:
        """刷新单个账号的 OAuth 令牌（OEP ``POST /api/accounts/<id>/refresh``）。

        该接口成功/失败都返回 ``200``，靠 payload 的 ``success`` 与 ``error.code`` 区分。
        """
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            raise _contract_error()
        data = await self._json_request(
            "POST", base_url, secret, f"/api/accounts/{account_id}/refresh"
        )
        if data.get("success") is True:
            return {"account_id": account_id, "status": "refreshed"}
        if data.get("success") is not False:
            raise _contract_error()
        error = data.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if code == "ACCOUNT_NOT_FOUND":
            raise SourceAdminError("SOURCE_NOT_FOUND")
        raise SourceAdminError("SOURCE_REJECTED")

    async def export_accounts(
        self, base_url: str, secret: str, config: dict | None = None
    ) -> str:
        """导出账号清单（OEP 二次验证 + 文本附件）。

        先 ``POST /api/export/verify {"password": secret}`` 换一次性 ``verify_token``，
        再 ``GET /api/accounts/export`` 带 ``X-Export-Token``。返回纯文本内容；
        内容含明文凭据，调用方不得写入日志或回显。
        """
        cookies = await self._login(base_url, secret)
        verify_response = await self._do(
            "POST",
            base_url.rstrip("/") + "/api/export/verify",
            cookies=cookies,
            json={"password": secret},
        )
        verify = self._envelope(verify_response)
        token = verify.get("verify_token")
        if verify.get("success") is not True or not isinstance(token, str) or not token:
            raise _contract_error()
        response = await self._do(
            "GET",
            base_url.rstrip("/") + "/api/accounts/export",
            cookies=cookies,
            headers={"X-Export-Token": token},
        )
        return response.text
