"""管理接口 `/icloud-api/admin/*`（给 `icloud_web` 与运维用）。

账号增删、凭据写入、浏览器授权发起/查询都在这里。列表与详情只回脱敏视图（`has_*` 布尔）。

鉴权说明：本批先用运营 Bearer token 守卫，登录会话 + CSRF 随 `icloud_web` 在 D5 落地
（见 docs/superpowers/specs/2026-10-12-phase-d-icloud-engine-design.md 进度台账），
规格里的 URL 契约不变。
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from icloud_api.accounts import encrypt_text, redacted_account, resolve_key
from icloud_api.auth import require_operator
from icloud_api.browser_auth import (
    BrowserAuthError,
    browser_auth_status,
    start_browser_auth,
)
from icloud_api.db import get_db
from icloud_api.models import (
    ICloudAccount,
    ICloudAlias,
    ICloudBrowserAuthSession,
    ICloudSchedulerJob,
)

router = APIRouter(prefix="/icloud-api/admin", dependencies=[Depends(require_operator)])


class CreateAccountRequest(BaseModel):
    name: str = Field(default="", max_length=128)
    host: str = Field(default="icloud.com", max_length=128)
    proxy: str = Field(default="", max_length=256)
    app_password: str = Field(default="", max_length=256)


class DeleteAccountRequest(BaseModel):
    # 删除不可逆，必须显式确认（规格：破坏性操作要求 confirm）。
    confirm: bool = False


def _require_account(db: Session, account_id: str) -> ICloudAccount:
    account = db.get(ICloudAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


@router.post("/accounts")
def create_account(payload: CreateAccountRequest, request: Request, db: Annotated[Session, Depends(get_db)]) -> dict:
    key = resolve_key(request.app.state.settings.data_key)
    account = ICloudAccount(
        name=payload.name.strip(),
        host=(payload.host or "icloud.com").strip() or "icloud.com",
        proxy=payload.proxy.strip(),
        app_password_enc=encrypt_text(key, payload.app_password),
    )
    db.add(account)
    db.commit()
    return redacted_account(account)


@router.get("/accounts")
def list_accounts(db: Annotated[Session, Depends(get_db)]) -> dict:
    rows = db.scalars(select(ICloudAccount).order_by(ICloudAccount.created_at)).all()
    return {"accounts": [redacted_account(row) for row in rows], "count": len(rows)}


@router.get("/accounts/{account_id}")
def get_account(account_id: str, db: Annotated[Session, Depends(get_db)]) -> dict:
    return redacted_account(_require_account(db, account_id))


@router.delete("/accounts/{account_id}")
def delete_account(
    account_id: str,
    payload: DeleteAccountRequest,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """删除本地账号与它在本地的库存/授权记录。

    不会去 Apple 侧删除已经建好的别名——上游别名要么先在界面上删掉，
    要么保留（转发设置仍在 Apple 账号里）。
    """
    _require_account(db, account_id)
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="删除账号不可逆，需要 confirm=true")
    # 用显式 DELETE 按依赖顺序执行：模型里只有外键列、没有 relationship，
    # 交给 ORM 排序会在外键打开时报 constraint failed。
    removed_aliases = db.execute(delete(ICloudAlias).where(ICloudAlias.account_id == account_id)).rowcount
    removed_sessions = db.execute(
        delete(ICloudBrowserAuthSession).where(ICloudBrowserAuthSession.account_id == account_id)
    ).rowcount
    removed_jobs = db.execute(
        delete(ICloudSchedulerJob).where(ICloudSchedulerJob.account_id == account_id)
    ).rowcount
    db.execute(delete(ICloudAccount).where(ICloudAccount.id == account_id))
    db.commit()
    return {
        "deleted": True,
        "account_id": account_id,
        "removed_aliases": removed_aliases,
        "removed_sessions": removed_sessions,
        "removed_jobs": removed_jobs,
        "upstream_aliases_untouched": True,
    }


@router.post("/accounts/{account_id}/browser-auth/start")
def start_browser_auth_for_account(
    account_id: str, db: Annotated[Session, Depends(get_db)]
) -> dict:
    account = _require_account(db, account_id)
    started = start_browser_auth(db, account)
    db.commit()
    return {
        "request_id": started.request_id,
        "authorization_code": started.authorization_code,
        "expires_at": started.expires_at,
    }


@router.get("/accounts/{account_id}/browser-auth/status")
def browser_auth_status_for_account(
    account_id: str,
    request_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    _require_account(db, account_id)
    try:
        row = browser_auth_status(db, account_id, request_id, now=datetime.now(UTC))
    except BrowserAuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return {
        "request_id": row.request_id,
        "status": row.status,
        "error": row.last_error,
        "expires_at": row.expires_at,
    }
