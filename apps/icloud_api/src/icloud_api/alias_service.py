"""别名写操作服务（对应 Go `server.go` 的 `createAliasPreferAppleAccount`）。

优先级：Apple Account 新接口（约 20 个/小时）> iCloud Web 旧接口（约 5 个/小时），
两接口配额独立。仅在新接口确实不可用时回退；新接口明确提示「总容量已满」时不回退
（总容量是账号级限额，两个接口共用，回退没有意义）。

每次 Apple Account 尝试后把会话状态写回账号（`apple_session_enc`），
让后续调用拿到最新的 scnt / apiKey / TTL。
"""

from dataclasses import dataclass
from typing import Any

from icloud_api.accounts import account_apple_session, account_cookies, encrypt_json
from icloud_api.aliases import (
    Alias,
    AliasError,
    AliasLimitReached,
    AliasRateLimited,
    alias_retry_after,
    root_alias_error,
)
from icloud_api.apple_account import (
    AppleAccountAuthFailed,
    AppleAccountClient,
    AppleAccountState,
)
from icloud_api.hme_client import HmeClient
from icloud_api.models import ICloudAccount

# 与 hme.ManagedCapacityTarget 一致：只是请求管理目标，不代表 Apple 实际配额。
MANAGED_CAPACITY_TARGET = 750

# 旧接口会话失效，但新接口（Apple Account）仍可继续创建时，跳过旧接口别名列表直接走新接口。
# 若新接口也确实不可用，直接透传错误。


@dataclass(frozen=True)
class CreateOutcome:
    """一次创建的结果。`interface` 标记实际走的是哪个接口（apple_account | icloud_web）。"""

    email: str
    label: str
    created_at: str
    interface: str


class AliasServiceError(Exception):
    """别名操作失败。`code` 是给调用方的稳定错误码。"""

    def __init__(self, code: str, message: str = "", retry_after: float = 0.0) -> None:
        self.code = code
        self.message = message
        self.retry_after = retry_after
        super().__init__(message or code)


def classify(error: BaseException) -> AliasServiceError:
    """把上游异常翻译成给调用方的稳定错误码（路由据此选 HTTP 状态）。

    必须先穿透包装层：上游客户端重试失败后会重新包一层错误，分类挂在 `__cause__` 上，
    只认最外层会把限速/容量满误判成普通失败（返回值就从 429/409 退化成 502）。
    """
    root = root_alias_error(error)
    message = getattr(root, "message", "") or getattr(error, "message", "") or str(error)
    if isinstance(root, AliasLimitReached):
        return AliasServiceError("ALIAS_LIMIT_REACHED", message or "隐藏邮箱总容量已满")
    if isinstance(root, AliasRateLimited):
        return AliasServiceError(
            "ALIAS_RATE_LIMITED",
            message or "Apple 暂时限制了创建频率",
            retry_after=alias_retry_after(root).total_seconds(),
        )
    if isinstance(root, AppleAccountAuthFailed):
        return AliasServiceError(
            "SESSION_REQUIRED", message or "Apple Account 管理会话失效，请重新网页授权"
        )
    return AliasServiceError("ALIAS_CREATE_FAILED", message or root.__class__.__name__)


def _save_apple_session(account: ICloudAccount, key: bytes, state: AppleAccountState) -> None:
    """把 Apple Account 会话状态加密写回账号，持久化由调用方提交。"""
    account.apple_session_enc = encrypt_json(key, state.to_dict())


def _apple_client(account: ICloudAccount, key: bytes, transport: Any) -> AppleAccountClient:
    """按账号配置构造 Apple Account 客户端；origin 交给客户端按 host 推导。"""
    state = AppleAccountState.from_dict(account_apple_session(account, key))
    state.host = state.host or account.host or "icloud.com"
    return AppleAccountClient(state, transport=transport)


def _web_client(account: ICloudAccount, key: bytes, transport: Any) -> HmeClient:
    """按账号配置构造 iCloud Web（旧接口）客户端。"""
    return HmeClient(
        cookies=account_cookies(account, key),
        host=account.host or "icloud.com",
        proxy=account.proxy,
        transport=transport,
    )


async def create_alias(
    account: ICloudAccount,
    key: bytes,
    *,
    label: str = "",
    transport: Any = None,
) -> CreateOutcome:
    """创建一个别名：Apple Account 优先，不可用时回退 iCloud Web。

    返回后调用方负责 commit；Apple Account 会话已写回 `apple_session_enc`。
    """
    aa = _apple_client(account, key, transport)
    try:
        result = await aa.create_alias(label)
    except AliasLimitReached:
        # 总容量已满 → 账号级限额，回退旧接口也不会成功
        raise
    except (AppleAccountAuthFailed, AliasRateLimited, AliasError):
        # 会话失效 / 限速 / 一般失败 → 记录已刷新的会话状态，再回退旧接口
        _save_apple_session(account, key, aa.current_state())
        return await _create_via_icloud_web(account, key, label=label, transport=transport)
    _save_apple_session(account, key, aa.current_state())
    return CreateOutcome(
        email=result.email,
        label=result.label,
        created_at=result.created_at,
        interface="apple_account",
    )


async def _create_via_icloud_web(
    account: ICloudAccount, key: bytes, *, label: str, transport: Any
) -> CreateOutcome:
    """回退旧接口（iCloud Web，约 5 个/小时）。"""
    client = _web_client(account, key, transport)
    async with client:
        result = await client.create_alias(label, max_retries=5)
    return CreateOutcome(
        email=result.email,
        label=result.label,
        created_at=result.created_at,
        interface="icloud_web",
    )


async def set_alias_state(
    account: ICloudAccount,
    key: bytes,
    anonymous_id: str,
    *,
    activate: bool,
    transport: Any = None,
) -> bool:
    """启用/停用别名（旧接口）。返回上游是否真的改了状态。"""
    client = _web_client(account, key, transport)
    async with client:
        if activate:
            return await client.reactivate(anonymous_id)
        return await client.deactivate(anonymous_id)


async def delete_alias(
    account: ICloudAccount,
    key: bytes,
    anonymous_id: str,
    transport: Any = None,
) -> None:
    """删除别名（旧接口，不可逆）。"""
    client = _web_client(account, key, transport)
    async with client:
        await client.delete(anonymous_id)


async def list_aliases(
    account: ICloudAccount,
    key: bytes,
    *,
    transport: Any = None,
) -> list[Alias]:
    """读取账号当前的隐藏邮箱列表（上游为准，返回完整快照）。

    只走 iCloud Web 旧接口：Apple Account 新接口目前只提供创建、没有列表，
    所以这里没有「新接口优先」的回退顺序——会话失效时如实抛 SESSION_REQUIRED，
    由路由映射成 401，而不是拿一份空列表冒充「上游没有别名」
    （那会把 C 站库存整片标成幽灵行）。
    """
    client = _web_client(account, key, transport)
    async with client:
        return await client.list_aliases()