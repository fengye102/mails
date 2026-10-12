"""机器接口（A/C/注册机调用）。

能力清单分两栏如实上报：`shipped` = 代码已落地，`verified_live` = 已在真实 Apple 账号上跑通。
没做完的链路一律 `shipped: false`，避免调用方以为某条链路可用；
还没上真机验证的链路 `verified_live: false`，调用方可以据此决定是否先用假上游/降级路径。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from icloud_api.accounts import resolve_key
from icloud_api.alias_inventory import apply_alias_snapshot
from icloud_api.alias_service import (
    AliasServiceError,
    classify,
    create_alias,
    delete_alias,
    list_aliases,
    set_alias_state,
)
from icloud_api.auth import require_operator
from icloud_api.db import get_db
from icloud_api.mail import service as mail_service
from icloud_api.models import ICloudAccount, ICloudAlias, utc_now

router = APIRouter(prefix="/icloud-api/v1", dependencies=[Depends(require_operator)])

# 每个子批次落地后改这里；调用方按这个清单决定走哪条链路。
SHIPPED_FEATURES: dict[str, bool] = {
    "aliases_read": True,  # D1：会话校验 + 别名/转发设置读取（含假上游测试）
    "aliases_write": True,  # D4：建/停/启用/删除别名（Apple Account 优先，旧接口回退）
    "mail_imap": True,  # D3：App Password IMAP 读信（路由已暴露；真机验证待账号）
    "mail_web": False,  # D3
    "mail_netease_web": False,  # 明确不移植（163 网页取件不做）
    "browser_auth": True,  # D2：一次性授权码 + Cookie 回填校验（163 收件箱网页授权不移植）
    "apple_account_session": True,  # D4：account.apple.com 会话（bootstrap/refresh/建别名）
    "apple_srp_login": True,  # D2：SRP 登录会话（authStart→2FA→管理门户，真机验证待账号）
    "scheduler": True,  # D4：定时创建（默认关，须界面显式打开；回退与停用逻辑已覆盖）
}

# 只有拿真实账号跑通过的链路才进来；上真机前一律留在外面。
LIVE_VERIFIED_FEATURES: frozenset[str] = frozenset()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "icloud-hme-engine"}


def _require_account(db: Session, account_id: str) -> ICloudAccount:
    account = db.get(ICloudAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


@router.get("/accounts/{account_id}/mail")
async def list_account_mail(
    account_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    address: Annotated[str, Query(max_length=320)] = "",
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    days: Annotated[int | None, Query(ge=0, le=365)] = None,
) -> dict:
    """按别名地址读信（IMAP 优先）。Web 回退随 mail_web 落地后接入。"""
    account = _require_account(db, account_id)
    key = resolve_key(request.app.state.settings.data_key)
    factory = request.app.state.imap_client_factory
    try:
        # imaplib 是同步阻塞的，丢线程池避免卡事件循环。
        return await run_in_threadpool(
            mail_service.list_account_mail,
            db,
            account,
            key,
            address=address,
            limit=limit,
            days=days,
            client_factory=factory,
        )
    except mail_service.MailServiceError as error:
        raise HTTPException(status_code=_mail_error_status(error.code), detail=error.message) from error


def _mail_error_status(code: str) -> int:
    if code in ("MAIL_NO_EMAIL", "MAIL_NOT_ICLOUD_EMAIL", "MAIL_NO_APP_PASSWORD"):
        return 400  # 预检失败：账号配置缺失，调用方补配置后重试
    if code == "IMAP_LOGIN_FAILED":
        return 401  # App Password 无效
    if code in ("IMAP_CONFIG_INVALID", "IMAP_NOT_CONNECTED"):
        return 400
    return 502  # 其余视为上游 IMAP 故障


class CreateAliasRequest(BaseModel):
    """建一个隐藏邮箱。label 留空时上游自己随机生成。"""

    label: str = Field(default="", max_length=128)


class AliasStateRequest(BaseModel):
    """停用/启用/删除一个别名。"""

    anonymous_id: str = Field(min_length=1, max_length=128)


def _alias_error_http(error: AliasServiceError) -> HTTPException:
    """把写操作失败映射成对调用方有用的 HTTP 语义。"""
    if error.code == "ALIAS_LIMIT_REACHED":
        return HTTPException(status_code=409, detail=error.message)  # 总容量已满
    if error.code == "ALIAS_RATE_LIMITED":
        # 429 带 Retry-After，调用方（含调度器）能按建议时长退避。
        headers = {"Retry-After": str(int(max(error.retry_after, 1)))} if error.retry_after > 0 else None
        return HTTPException(status_code=429, detail=error.message, headers=headers)
    if error.code == "SESSION_REQUIRED":
        return HTTPException(status_code=401, detail=error.message)
    return HTTPException(status_code=502, detail=error.message)


@router.get("/accounts/{account_id}/aliases")
async def list_account_aliases(
    account_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """列出账号的隐藏邮箱（以 Apple 上游为准），并把快照同步进本地库存。

    以「上游快照」为准的理由：本地库存只是缓存，Apple 侧才是真相；
    调用方（C 站库存同步）拿到的必须是当前真实存在的别名，否则会发出已经失效的邮箱。

    上游已消失的本地行标记 `state="missing"` 而不是删除——引擎看不到 C 站的分配关系，
    「上游没了」不等于「可以回收」（设计规格不变量 6）。

    分页只作用于**响应**：幽灵行判定必须基于完整快照，只看一页会把「不在本页」
    误判成「上游已删」，一次同步就能把整账号库存标成幽灵行。
    """
    account = _require_account(db, account_id)
    key = resolve_key(request.app.state.settings.data_key)
    transport = getattr(request.app.state, "icloud_transport", None)
    try:
        upstream = await list_aliases(account, key, transport=transport)
    except Exception as error:
        raise _alias_error_http(classify(error)) from error

    outcome = apply_alias_snapshot(db, account_id=account.id, upstream=upstream)
    db.commit()
    return {
        "account_id": account.id,
        "count": len(upstream),
        "total": len(upstream),
        "limit": limit,
        "offset": offset,
        "synced": outcome.as_dict(),
        "aliases": [
            {
                "address": alias.email,
                "anonymous_id": alias.anonymous_id,
                "label": alias.label,
                "active": alias.active,
                "created_at": alias.created_at,
                "forward_to_email": alias.forward_to_email,
            }
            for alias in upstream[offset : offset + limit]
        ],
    }


@router.post("/accounts/{account_id}/aliases")
async def create_account_alias(
    account_id: str,
    payload: CreateAliasRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """建一个隐藏邮箱：Apple Account 新接口优先，不可用时自动回退 iCloud Web 旧接口。

    建完在本地库存里补一条记录（D5 的 C 站点同步会把引擎库存同步过去）。
    上游真实配额由 Apple 决定，这里不预检；`anonymous_id` 等下次同步时补齐。
    """
    account = _require_account(db, account_id)
    key = resolve_key(request.app.state.settings.data_key)
    transport = getattr(request.app.state, "icloud_transport", None)
    try:
        outcome = await create_alias(
            account, key, label=payload.label.strip(), transport=transport
        )
    except Exception as error:
        raise _alias_error_http(classify(error)) from error

    db.add(
        ICloudAlias(
            account_id=account.id,
            address=outcome.email,
            label=outcome.label,
            state="active",
        )
    )
    db.commit()
    return {
        "email": outcome.email,
        "label": outcome.label,
        "created_at": outcome.created_at,
        "account_id": account.id,
        "interface": outcome.interface,
    }


@router.post("/accounts/{account_id}/aliases/{anonymous_id}/deactivate")
async def deactivate_account_alias(
    account_id: str,
    anonymous_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """停用一个别名（转发仍保留，可随时再启用）。"""
    account = _require_account(db, account_id)
    key = resolve_key(request.app.state.settings.data_key)
    transport = getattr(request.app.state, "icloud_transport", None)
    try:
        changed = await set_alias_state(
            account, key, anonymous_id, activate=False, transport=transport
        )
    except Exception as error:
        raise _alias_error_http(classify(error)) from error
    _mark_local_state(db, account_id, anonymous_id, "inactive")
    return {"account_id": account_id, "anonymous_id": anonymous_id, "state": "inactive", "changed": changed}


@router.post("/accounts/{account_id}/aliases/{anonymous_id}/reactivate")
async def reactivate_account_alias(
    account_id: str,
    anonymous_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """重新启用一个别名。"""
    account = _require_account(db, account_id)
    key = resolve_key(request.app.state.settings.data_key)
    transport = getattr(request.app.state, "icloud_transport", None)
    try:
        changed = await set_alias_state(
            account, key, anonymous_id, activate=True, transport=transport
        )
    except Exception as error:
        raise _alias_error_http(classify(error)) from error
    _mark_local_state(db, account_id, anonymous_id, "active")
    return {"account_id": account_id, "anonymous_id": anonymous_id, "state": "active", "changed": changed}


@router.delete("/accounts/{account_id}/aliases/{anonymous_id}")
async def delete_account_alias(
    account_id: str,
    anonymous_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """删除一个别名（上游不可逆）。

    删除后本地库存里对应行一并清掉——留着会变成幽灵行，下次同步时又会被当新增加回来。
    """
    account = _require_account(db, account_id)
    key = resolve_key(request.app.state.settings.data_key)
    transport = getattr(request.app.state, "icloud_transport", None)
    try:
        await delete_alias(account, key, anonymous_id, transport=transport)
    except Exception as error:
        raise _alias_error_http(classify(error)) from error
    removed = len(
        list(
            db.scalars(
                select(ICloudAlias).where(
                    ICloudAlias.account_id == account_id,
                    ICloudAlias.anonymous_id == anonymous_id,
                )
            )
        )
    )
    db.execute(
        delete(ICloudAlias).where(
            ICloudAlias.account_id == account_id,
            ICloudAlias.anonymous_id == anonymous_id,
        )
    )
    db.commit()
    return {
        "account_id": account_id,
        "anonymous_id": anonymous_id,
        "deleted": True,
        "removed_local_rows": removed,
    }


def _mark_local_state(db: Session, account_id: str, anonymous_id: str, state: str) -> None:
    """把本地库存里对应别名的状态改掉；行不在本地时静默跳过（下次同步补齐）。"""
    row = db.scalars(
        select(ICloudAlias).where(
            ICloudAlias.account_id == account_id,
            ICloudAlias.anonymous_id == anonymous_id,
        )
    ).first()
    if row is not None:
        row.state = state
        row.synced_at = utc_now()
    db.commit()


@router.get("/capabilities")
def capabilities(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "service": "icloud-hme-engine",
        "features": {
            name: {"shipped": shipped, "verified_live": name in LIVE_VERIFIED_FEATURES}
            for name, shipped in SHIPPED_FEATURES.items()
        },
        "limits": {
            "alias_min_interval_seconds": settings.alias_min_interval_seconds,
            "scheduler_default_enabled": settings.scheduler_default_enabled,
            "browser_auth_ttl_seconds": settings.browser_auth_ttl_seconds,
        },
    }
