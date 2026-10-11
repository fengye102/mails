"""来源管理适配器的共享契约。

来源管理只做「管理动作」（建地址、加账号、刷新令牌、删除等），不参与收件箱同步：
它复用连接里已经加密保存的来源凭据，读邮件链路完全不受影响，也不新增数据库表。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


class SourceAdminError(Exception):
    """管理动作失败。code 是固定错误码，直接映射为 HTTP detail。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# 管理动作统一使用的固定错误码；新增来源沿用同一套，不新增自定义字符串。
SOURCE_ERROR_CODES = (
    "SOURCE_AUTH",
    "SOURCE_TIMEOUT",
    "SOURCE_UNAVAILABLE",
    "SOURCE_CONTRACT",
    "SOURCE_NOT_FOUND",
    "SOURCE_CONFLICT",
    "SOURCE_REJECTED",
)


@dataclass
class SourceItem:
    """来源里的一个可管理对象（地址或账号）的统一视图。"""

    provider_key: str
    address: str
    status: str
    created_at: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "provider_key": self.provider_key,
            "address": self.address,
            "status": self.status,
            "created_at": self.created_at,
            "detail": self.detail,
        }


class SourceAdmin(Protocol):
    """来源管理适配器协议；来源专有动作由各自模块补充方法，路由层直接调用。"""

    async def probe(self, base_url: str, secret: str, config: dict | None = None) -> None: ...

    async def list_items(
        self, base_url: str, secret: str, config: dict | None = None
    ) -> list[SourceItem]: ...

    async def create_item(
        self, base_url: str, secret: str, payload: dict[str, Any], config: dict | None = None
    ) -> SourceItem: ...

    async def delete_item(
        self, base_url: str, secret: str, provider_key: str, config: dict | None = None
    ) -> None: ...
