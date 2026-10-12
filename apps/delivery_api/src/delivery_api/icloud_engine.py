"""iCloud 引擎机器接口的 C 站客户端（只读）。

C 站不直接跟 Apple 打交道：别名快照从引擎的 `/icloud-api/v1/*` 取，
C 站只负责「把这批别名当库存管起来」。

约定：
- 只发 GET，C 站永远不改上游状态（停用/删除别名是引擎管理接口的事）；
- 运营 token 只出现在请求头，不进日志、不进响应、不进异常消息；
- 上游错误一律收敛成固定错误码（`EngineError.code`），调用方按码映射 HTTP 状态。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

# 引擎侧上限（apps/icloud_api 的别名路由：limit ≤ 1000）。
_ALIAS_PAGE = 1000
_MAX_ALIASES = 10_000


@dataclass(frozen=True)
class AliasSnapshot:
    """上游一条别名的规范视图：地址已归一化，`active` 为上游是否可用。"""

    address: str
    active: bool


_ENGINE_SUMMARIES = {
    "UPSTREAM_AUTH": "引擎拒绝了这个运营 token。",
    "UPSTREAM_TIMEOUT": "引擎请求超时。",
    "UPSTREAM_UNAVAILABLE": "引擎不可用。",
    "UPSTREAM_CONTRACT": "引擎响应不符合读契约。",
}


class EngineError(Exception):
    """引擎调用失败。`code` 是固定错误码，`summary` 是不含凭据的说明。"""

    def __init__(self, code: str) -> None:
        self.code = code
        self.summary = _ENGINE_SUMMARIES.get(code, "引擎调用失败。")
        super().__init__(self.summary)


def _contract_error() -> EngineError:
    return EngineError("UPSTREAM_CONTRACT")


class ICloudEngineClient:
    """引擎机器接口的最小只读客户端。"""

    def __init__(self, client: httpx.Client, base_url: str, token: str, timeout: float = 20.0):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            response = self.client.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                params=params,
                timeout=self.timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise EngineError("UPSTREAM_TIMEOUT") from None
        except httpx.HTTPError:
            # 连接错误也会带 URL；固定摘要，不把上游细节抛给调用方。
            raise EngineError("UPSTREAM_UNAVAILABLE") from None
        if response.status_code in (401, 403):
            raise EngineError("UPSTREAM_AUTH")
        if response.status_code >= 500:
            raise EngineError("UPSTREAM_UNAVAILABLE")
        if response.status_code != 200:
            raise _contract_error()
        try:
            payload = response.json()
        except ValueError:
            raise _contract_error() from None
        if not isinstance(payload, dict):
            raise _contract_error()
        return payload

    def list_aliases(self, account_id: str) -> list[AliasSnapshot]:
        """取账号的别名全量快照（分页拉完）。

        幽灵行判定要用**完整快照**：只看一页会把「不在本页」误判成「上游已删」，
        一次同步就能把整账号库存标成 missing。所以这里翻页翻到底再返回。

        上游形状在这里校验并归一化：上层只面对 `AliasSnapshot`，
        字段缺失/类型不对一律当契约错（不猜默认值）。
        """
        if not isinstance(account_id, str) or not account_id.strip():
            raise _contract_error()
        aliases: list[AliasSnapshot] = []
        seen: set[str] = set()
        offset = 0
        fetched = 0
        while True:
            payload = self._get(
                f"/icloud-api/v1/accounts/{account_id.strip()}/aliases",
                params={"limit": _ALIAS_PAGE, "offset": offset},
            )
            page = payload.get("aliases")
            if not isinstance(page, list) or len(page) > _ALIAS_PAGE:
                raise _contract_error()
            fetched += len(page)
            if fetched > _MAX_ALIASES:
                # 按「实际取到的条数」封顶，而不是去重后的条数：
                # 上游若反复返回同一页（offset 被忽略），去重后条数永远不涨，
                # 只数去重结果会变成死循环。宁可报契约错，也不要挂着不放。
                raise _contract_error()
            for item in page:
                snapshot = _parse_alias(item)
                # 上游同地址重复只算一次，避免同一行被 upsert 两遍。
                if snapshot.address not in seen:
                    seen.add(snapshot.address)
                    aliases.append(snapshot)
            if len(page) < _ALIAS_PAGE:
                return aliases
            offset += _ALIAS_PAGE


    def list_mail(
        self, account_id: str, address: str, *, limit: int = 50, days: int = 7
    ) -> list[dict]:
        """读某个别名的邮件（引擎侧会真连一次 IMAP）。

        返回上游原始消息字典，字段归一化交给入库层（`messages.normalize_message`
        认 id/sender/to/subject/date/body_text），C 站不在这里重写一套映射。
        """
        if not isinstance(account_id, str) or not account_id.strip():
            raise _contract_error()
        target = (address or "").strip().lower()
        if not target:
            raise _contract_error()
        payload = self._get(
            f"/icloud-api/v1/accounts/{account_id.strip()}/mail",
            params={"address": target, "limit": limit, "days": days},
        )
        echo = payload.get("address")
        if not isinstance(echo, str) or echo.strip().lower() != target:
            # 回显不一致 = 取到的不是这封地址的信。宁可报错，也不把别人的邮件塞进取件箱。
            raise _contract_error()
        messages = payload.get("messages")
        if not isinstance(messages, list) or len(messages) > 200:
            raise _contract_error()
        if any(not isinstance(item, dict) for item in messages):
            raise _contract_error()
        return messages


def _parse_alias(item: object) -> AliasSnapshot:
    if not isinstance(item, dict):
        raise _contract_error()
    address = item.get("address")
    active = item.get("active")
    if not isinstance(address, str) or not address.strip() or "\x00" in address:
        raise _contract_error()
    if not isinstance(active, bool):
        raise _contract_error()
    return AliasSnapshot(address=address.strip().lower(), active=active)
