"""CYMail（iCloud 隐藏邮箱 / Hide My Email）来源管理适配器。

只负责「管理动作」：探测、列出/创建/删除 HME 别名、停用/启用别名、账号列表与库存同步。
复用连接里已加密保存的内部密钥（X-API-Key），不参与收件箱同步，也不写任何持久数据。

上游契约（以 /opt/cymail/admin-server 源码为准）：所有响应统一为
``{"success": bool, "data": ...}``；密钥失败一律映射为固定错误码，绝不回传上游 message 原文。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from mails_api.sources_admin.base import SourceAdminError, SourceItem

_TIMEOUT_SECONDS = 20.0
_UNSAFE_KEY_CHARS = "/?#\\"


def build(client: httpx.AsyncClient) -> CYMailSourceAdmin:
    """``sources_admin.admin_for`` 依赖的工厂。"""
    return CYMailSourceAdmin(client)


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
    """别名的 :id 路径参数（anonymousId 或地址）；拒绝路径穿越与控制字符。"""
    text = _text(value, required=True).strip()
    if len(text) > 256 or any(ch in text for ch in _UNSAFE_KEY_CHARS):
        raise _contract_error()
    if any(ord(ch) < 32 for ch in text):
        raise _contract_error()
    return text


def _iso_utc(value: Any) -> str | None:
    """把上游时间规范化为 UTC ISO8601（Z 结尾）；缺失返回 None。"""
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


class CYMailSourceAdmin:
    """CYMail 管理适配器；所有方法都复用同一 ``X-API-Key`` 内部密钥。"""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    # ---- 传输层 ----

    async def _request(
        self,
        method: str,
        base_url: str,
        secret: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self.client.request(
                method,
                base_url.rstrip("/") + path,
                headers={"X-API-Key": secret},
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
            # 400/429 等上游明确拒绝；不含上游 message，避免敏感内容外泄。
            raise SourceAdminError("SOURCE_REJECTED")
        return self._envelope(response)

    @staticmethod
    def _envelope(response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            raise _contract_error() from None
        if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool):
            raise _contract_error()
        if payload["success"] is not True:
            raise SourceAdminError("SOURCE_REJECTED")
        return payload.get("data")

    @staticmethod
    def _account_id(config: dict | None) -> str:
        account_id = (config or {}).get("account_id")
        if not isinstance(account_id, str) or not account_id.strip():
            raise _contract_error()
        return account_id.strip()

    # ---- 协议三件套 ----

    async def probe(self, base_url: str, secret: str, config: dict | None = None) -> None:
        data = await self._request("GET", base_url, secret, "/api/accounts")
        if not isinstance(data, list):
            raise _contract_error()

    async def list_items(
        self, base_url: str, secret: str, config: dict | None = None
    ) -> list[SourceItem]:
        account_id = self._account_id(config)
        data = await self._request(
            "GET", base_url, secret, "/api/aliases", params={"account_id": account_id}
        )
        if not isinstance(data, dict) or data.get("account_id") != account_id:
            raise _contract_error()
        aliases = data.get("aliases")
        if not isinstance(aliases, list):
            raise _contract_error()
        items: list[SourceItem] = []
        for alias in aliases:
            if not isinstance(alias, dict):
                raise _contract_error()
            address = _text(alias.get("email"), required=True)
            anonymous_id = _text(alias.get("anonymousId"), required=True)
            active = alias.get("active")
            if not isinstance(active, bool):
                raise _contract_error()
            items.append(
                SourceItem(
                    provider_key=anonymous_id,
                    address=address,
                    status="active" if active else "inactive",
                    created_at=_iso_utc(alias.get("createdAt")),
                    detail={
                        "label": _optional_text(alias.get("label")),
                        "anonymous_id": anonymous_id,
                        "active": active,
                        "account_id": account_id,
                    },
                )
            )
        return items

    async def create_item(
        self,
        base_url: str,
        secret: str,
        payload: dict[str, Any],
        config: dict | None = None,
    ) -> SourceItem:
        account_id = self._account_id(config)
        if not isinstance(payload, dict):
            raise _contract_error()
        label = payload.get("label", "")
        if not isinstance(label, str) or "\x00" in label:
            raise _contract_error()
        label = label.strip()
        if len(label) > 256:
            raise _contract_error()
        body: dict[str, Any] = {"account_id": account_id}
        if label:
            body["label"] = label
        data = await self._request("POST", base_url, secret, "/api/aliases", json=body)
        if not isinstance(data, dict):
            raise _contract_error()
        address = _text(data.get("email"), required=True)
        # 创建响应不含 anonymousId；以地址作为 provider_key，detail 里带 label/interface。
        return SourceItem(
            provider_key=address,
            address=address,
            status="active",
            created_at=_iso_utc(data.get("created_at")),
            detail={
                "label": _optional_text(data.get("label")),
                "account_id": account_id,
                "interface": _optional_text(data.get("interface")),
            },
        )

    async def delete_item(
        self,
        base_url: str,
        secret: str,
        provider_key: str,
        config: dict | None = None,
    ) -> None:
        account_id = self._account_id(config)
        key = _key_text(provider_key)
        data = await self._request(
            "DELETE",
            base_url,
            secret,
            f"/api/aliases/{quote(key, safe='')}",
            json={"account_id": account_id},
        )
        if not isinstance(data, dict):
            raise _contract_error()

    # ---- 来源专有动作 ----

    async def list_accounts(
        self, base_url: str, secret: str, config: dict | None = None
    ) -> list[dict[str, Any]]:
        data = await self._request("GET", base_url, secret, "/api/accounts")
        if not isinstance(data, list):
            raise _contract_error()
        accounts: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                raise _contract_error()
            account = {
                "id": _text(item.get("id"), required=True),
                "name": _optional_text(item.get("name")),
                "host": _optional_text(item.get("host")),
                "status": _optional_text(item.get("status")),
            }
            for field in ("alias_total", "alias_active"):
                if isinstance(item.get(field), int) and not isinstance(item[field], bool):
                    account[field] = item[field]
            accounts.append(account)
        return accounts

    async def deactivate_alias(
        self,
        base_url: str,
        secret: str,
        provider_key: str,
        config: dict | None = None,
    ) -> bool:
        return await self._alias_action(base_url, secret, provider_key, config, "deactivate")

    async def reactivate_alias(
        self,
        base_url: str,
        secret: str,
        provider_key: str,
        config: dict | None = None,
    ) -> bool:
        return await self._alias_action(base_url, secret, provider_key, config, "reactivate")

    async def _alias_action(
        self,
        base_url: str,
        secret: str,
        provider_key: str,
        config: dict | None,
        action: str,
    ) -> bool:
        account_id = self._account_id(config)
        key = _key_text(provider_key)
        data = await self._request(
            "POST",
            base_url,
            secret,
            f"/api/aliases/{quote(key, safe='')}/{action}",
            json={"account_id": account_id},
        )
        if not isinstance(data, dict):
            raise _contract_error()
        flag = data.get("success")
        if flag is False:
            raise SourceAdminError("SOURCE_REJECTED")
        if flag is not True:
            raise _contract_error()
        return True

    async def sync_inventory(
        self, base_url: str, secret: str, config: dict | None = None
    ) -> dict[str, Any]:
        account_id = self._account_id(config)
        data = await self._request(
            "POST", base_url, secret, "/api/inventory/sync", json={"account_id": account_id}
        )
        if not isinstance(data, dict):
            raise _contract_error()
        synced = data.get("synced")
        if not isinstance(synced, int) or isinstance(synced, bool) or synced < 0:
            raise _contract_error()
        return {"account_id": account_id, "synced": synced}
