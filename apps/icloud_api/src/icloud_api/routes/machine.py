"""机器接口（A/C/注册机调用）。

能力清单分两栏如实上报：`shipped` = 代码已落地，`verified_live` = 已在真实 Apple 账号上跑通。
没做完的链路一律 `shipped: false`，避免调用方以为某条链路可用；
还没上真机验证的链路 `verified_live: false`，调用方可以据此决定是否先用假上游/降级路径。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from icloud_api.accounts import resolve_key
from icloud_api.auth import require_operator
from icloud_api.db import get_db
from icloud_api.mail import service as mail_service
from icloud_api.models import ICloudAccount

router = APIRouter(prefix="/icloud-api/v1", dependencies=[Depends(require_operator)])

# 每个子批次落地后改这里；调用方按这个清单决定走哪条链路。
SHIPPED_FEATURES: dict[str, bool] = {
    "aliases_read": True,  # D1：会话校验 + 别名/转发设置读取（含假上游测试）
    "aliases_write": False,  # D4：客户端方法已移植，但建/停/删接口未暴露
    "mail_imap": True,  # D3：App Password IMAP 读信（路由已暴露；真机验证待账号）
    "mail_web": False,  # D3
    "mail_netease_web": False,  # 明确不移植（163 网页取件不做）
    "browser_auth": True,  # D2：一次性授权码 + Cookie 回填校验（163 收件箱网页授权不移植）
    "apple_account_session": False,  # D4：account.apple.com 会话引导（提配额用），尚未移植
    "apple_srp_login": True,  # D1：SRP 数学与登录会话骨架
    "scheduler": False,  # D4
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
