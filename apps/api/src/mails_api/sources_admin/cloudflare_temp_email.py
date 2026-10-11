"""Cloudflare 临时邮箱（CF temp mail Worker）的来源管理适配器。

管理动作只做无状态的上游 HTTP 调用：建/删地址、清空收件箱、列/删邮件、查看/重置地址口令。
它复用连接里已加密保存的管理员口令，不落库、不参与收件箱同步。

上游契约以 Worker 源码 admin_api/ 为准，认证头为 x-admin-auth；管理员口令只出现在请求头里，
绝不写日志、绝不进异常消息。查看地址口令返回的是上游签发的凭据（jwt），按敏感字段处理。
"""

import asyncio
import hashlib
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from mails_api.sources_admin.base import SourceAdmin, SourceAdminError, SourceItem

ADMIN_AUTH_HEADER = "x-admin-auth"
USER_AGENT = "mails-admin/1.0"
# handleListQuery 只接受 1..100 的 limit，且分页时仅 offset=0 返回真实 count。
PAGE_SIZE = 100


def _contract() -> SourceAdminError:
    return SourceAdminError("SOURCE_CONTRACT")


def _identifier(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise _contract()
    return str(value)


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise _contract()
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise _contract()
    value = value.strip()
    return value or None


def _iso_utc(value: object) -> str:
    """D1 的 datetime('now') 是 UTC 但无时区标记；统一输出以 Z 结尾的 ISO8601。"""
    if not isinstance(value, str) or not value.strip():
        raise _contract()
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise _contract() from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


class CloudflareTempEmailAdmin:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _request(
        self,
        method: str,
        base_url: str,
        secret: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> httpx.Response:
        url = base_url.rstrip("/") + path
        for attempt in range(2):
            try:
                async with asyncio.timeout(20):
                    response = await self.client.request(
                        method,
                        url,
                        headers={ADMIN_AUTH_HEADER: secret, "User-Agent": USER_AGENT},
                        params=params,
                        json=json,
                        timeout=httpx.Timeout(20, connect=5),
                        follow_redirects=False,
                    )
            except (httpx.TimeoutException, TimeoutError):
                if attempt == 0:
                    await asyncio.sleep(0.2)
                    continue
                raise SourceAdminError("SOURCE_TIMEOUT") from None
            except httpx.RequestError:
                raise SourceAdminError("SOURCE_UNAVAILABLE") from None
            if 500 <= response.status_code <= 599:
                if attempt == 0:
                    await asyncio.sleep(0.2)
                    continue
                raise SourceAdminError("SOURCE_UNAVAILABLE")
            if response.status_code in (401, 403):
                raise SourceAdminError("SOURCE_AUTH")
            if response.status_code == 404:
                raise SourceAdminError("SOURCE_NOT_FOUND")
            if response.status_code == 409:
                raise SourceAdminError("SOURCE_CONFLICT")
            if not 200 <= response.status_code <= 299:
                raise SourceAdminError("SOURCE_REJECTED")
            return response
        raise _contract()

    @staticmethod
    def _object(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError:
            raise _contract() from None
        if not isinstance(payload, dict):
            raise _contract()
        return payload

    def _expect_success(self, response: httpx.Response) -> None:
        if self._object(response).get("success") is not True:
            raise _contract()

    async def probe(self, base_url: str, secret: str, config: dict | None = None) -> None:
        response = await self._request("GET", base_url, secret, "/health_check")
        if not response.text.strip():
            raise SourceAdminError("SOURCE_UNAVAILABLE")

    @staticmethod
    def _item(row: object) -> SourceItem:
        if not isinstance(row, dict):
            raise _contract()
        provider_key = _identifier(row.get("id"))
        address = _required_text(row.get("name"))
        detail: dict = {}
        source_meta = _optional_text(row.get("source_meta"))
        if source_meta is not None:
            detail["source_meta"] = source_meta
        for field in ("mail_count", "send_count"):
            value = row.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                detail[field] = value
        return SourceItem(
            provider_key=provider_key,
            address=address,
            status="active",
            created_at=_iso_utc(row.get("created_at")),
            detail=detail,
        )

    async def list_items(
        self, base_url: str, secret: str, config: dict | None = None
    ) -> list[SourceItem]:
        items: list[SourceItem] = []
        offset = 0
        for _ in range(1000):
            response = await self._request(
                "GET",
                base_url,
                secret,
                "/admin/address",
                params={"limit": PAGE_SIZE, "offset": offset},
            )
            payload = self._object(response)
            results = payload.get("results")
            count = payload.get("count")
            if (
                not isinstance(results, list)
                or len(results) > PAGE_SIZE
                or isinstance(count, bool)
                or not isinstance(count, int)
            ):
                raise _contract()
            items.extend(self._item(row) for row in results)
            if len(results) < PAGE_SIZE:
                return items
            offset += len(results)
        raise _contract()

    async def create_item(
        self, base_url: str, secret: str, payload: dict, config: dict | None = None
    ) -> SourceItem:
        if not isinstance(payload, dict):
            raise _contract()
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SourceAdminError("SOURCE_REJECTED")
        body: dict = {"name": name.strip(), "enablePrefix": bool(payload.get("enable_prefix", False))}
        domain = payload.get("domain")
        if domain is not None:
            if not isinstance(domain, str) or not domain.strip():
                raise SourceAdminError("SOURCE_REJECTED")
            body["domain"] = domain.strip()
        response = await self._request("POST", base_url, secret, "/admin/new_address", json=body)
        result = self._object(response)
        # 上游还会返回 jwt / password；它们是凭据，绝不放进管理视图。
        return SourceItem(
            provider_key=_identifier(result.get("address_id")),
            address=_required_text(result.get("address")),
            status="active",
            created_at=None,
            detail={},
        )

    async def delete_item(
        self, base_url: str, secret: str, provider_key: str, config: dict | None = None
    ) -> None:
        key = _identifier(provider_key)
        response = await self._request(
            "DELETE", base_url, secret, "/admin/delete_address/" + quote(key, safe="")
        )
        self._expect_success(response)

    async def clear_inbox(
        self, base_url: str, secret: str, provider_key: str, config: dict | None = None
    ) -> None:
        key = _identifier(provider_key)
        response = await self._request(
            "DELETE", base_url, secret, "/admin/clear_inbox/" + quote(key, safe="")
        )
        self._expect_success(response)

    @staticmethod
    def _mail(row: object) -> dict:
        if not isinstance(row, dict):
            raise _contract()
        return {
            "provider_key": _identifier(row.get("id")),
            "address": _optional_text(row.get("address")),
            "source": _optional_text(row.get("source")),
            "message_id": _optional_text(row.get("message_id")),
            "created_at": _iso_utc(row.get("created_at")) if row.get("created_at") else None,
            # raw/raw_blob 是完整邮件正文，默认不下发到管理视图，避免把邮件内容写进日志。
            "is_unread": bool(row.get("is_unread")),
        }

    async def list_mails(
        self,
        base_url: str,
        secret: str,
        address: str | None = None,
        limit: int = 50,
        offset: int = 0,
        config: dict | None = None,
    ) -> dict:
        if not 1 <= limit <= PAGE_SIZE or offset < 0:
            raise SourceAdminError("SOURCE_REJECTED")
        params: dict = {"limit": limit, "offset": offset}
        if address is not None:
            if not isinstance(address, str) or not address.strip():
                raise SourceAdminError("SOURCE_REJECTED")
            params["address"] = address.strip()
        response = await self._request("GET", base_url, secret, "/admin/mails", params=params)
        payload = self._object(response)
        results = payload.get("results")
        count = payload.get("count")
        if (
            not isinstance(results, list)
            or len(results) > PAGE_SIZE
            or isinstance(count, bool)
            or not isinstance(count, int)
        ):
            raise _contract()
        return {"count": count, "items": [self._mail(row) for row in results]}

    async def delete_mail(
        self, base_url: str, secret: str, mail_id: str, config: dict | None = None
    ) -> None:
        key = _identifier(mail_id)
        response = await self._request(
            "DELETE", base_url, secret, "/admin/mails/" + quote(key, safe="")
        )
        self._expect_success(response)

    async def show_password(
        self, base_url: str, secret: str, provider_key: str, config: dict | None = None
    ) -> dict:
        key = _identifier(provider_key)
        response = await self._request(
            "GET", base_url, secret, "/admin/show_password/" + quote(key, safe="")
        )
        jwt = _required_text(self._object(response).get("jwt"))
        # 显式标记为敏感字段：路由层原样返回给 owner，但绝不写日志、绝不进审计文本。
        return {"provider_key": key, "sensitive": True, "field": "jwt", "value": jwt}

    async def reset_password(
        self,
        base_url: str,
        secret: str,
        provider_key: str,
        password: str,
        config: dict | None = None,
    ) -> None:
        key = _identifier(provider_key)
        if not isinstance(password, str) or not password:
            raise SourceAdminError("SOURCE_REJECTED")
        # 上游 admin API 约定 password 字段是明文口令的 SHA-256 十六进制（见 address_api.resetPassword）。
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        response = await self._request(
            "POST",
            base_url,
            secret,
            "/admin/address/" + quote(key, safe="") + "/reset_password",
            json={"password": digest},
        )
        self._expect_success(response)


def build(client: httpx.AsyncClient) -> SourceAdmin:
    return CloudflareTempEmailAdmin(client)
