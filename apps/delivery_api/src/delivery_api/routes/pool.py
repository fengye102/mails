"""池路由：对外 ``/pool-api/v1/*``（沿用 OEP 契约）+ 运维 ``/delivery-api/v1/pool/*``。

对外路由的**所有**响应都必须走 OEP 信封（``pool.ok`` / ``pool.fail``），**包括错误**：
不能把 FastAPI 的 ``{"detail": ...}`` 漏出去，否则注册机解析不了错误码、只能当网络错重试。

运维路由走平台惯例（operator bearer + ``{"detail": {"code": ...}}``），它们不对外。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from delivery_api import pool
from delivery_api.auth import require_operator
from delivery_api.db import get_db
from delivery_api.models import InventoryMailbox, PoolApiKey, PoolClaim, utc_now
from delivery_api.settings import Settings

admin_router = APIRouter(prefix="/delivery-api/v1/pool")
public_router = APIRouter(prefix="/pool-api/v1")

DbSession = Annotated[Session, Depends(get_db)]
Operator = Annotated[None, Depends(require_operator)]


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _error_response(exc: pool.PoolError) -> JSONResponse:
    return JSONResponse(pool.fail(exc.error_code, str(exc), data=exc.data), status_code=exc.http_status)


def _audit_access(db: Session, request: Request, key: pool.PoolApiKey | None, *, status: str, code: str = "OK", **details) -> None:
    """访问留痕。

    只在**认出了 Key** 之后才写：出错也要留痕（限流、参数错、项目不匹配都值得看见），
    但匿名的 401 不写——否则任何公网扫描都能无限往审计表里灌行。
    """

    if key is None:
        return
    pool.audit(
        db,
        event_type=pool.EVENT_API_ACCESS,
        details={"endpoint": request.url.path, "status": status, "code": pool.public_code(code), "key_id": key.id, "key_name": key.name, **details},
    )
    db.commit()


# ================================================================== 对外：池接口


class PoolClaimRequest(BaseModel):
    """领取入参。

    字段一律宽松接收（OEP 是 ``body.get(...)`` 再用），真正的校验在 ``pool.validate_*``，
    这样错误码才对得上 OEP 的码表。
    """

    model_config = ConfigDict(extra="allow")
    caller_id: Any = None
    task_id: Any = None
    provider: Any = None
    project_key: Any = None
    email_domain: Any = None
    # 平台扩展（OEP 没有这个字段，老客户端不传即不受影响）：注册机重试同一任务时带上它，
    # 可以拿回同一条领取而不是再消耗一个邮箱。
    idempotency_key: Any = None


class PoolClaimActionRequest(BaseModel):
    """释放 / 回传入参。"""

    model_config = ConfigDict(extra="allow")
    account_id: Any = None
    claim_token: Any = None
    caller_id: Any = None
    task_id: Any = None
    project_key: Any = None
    reason: Any = None
    result: Any = None
    detail: Any = None


@public_router.get("/stats")
def pool_api_stats(request: Request, db: DbSession):
    """池状态。``pool_counts`` 的键与 OEP 一致；``unusable_available`` 是本平台附加的诊断项。"""

    settings = _settings(request)
    key: pool.PoolApiKey | None = None
    try:
        pool.ensure_pool_enabled(settings)
        key = pool.authenticate_pool_key(db, request)
        pool.enforce_rate_limit(request.app.state.pool_rate_limiter, key)
        data = pool.pool_stats(db, settings)
    except pool.PoolError as exc:
        _audit_access(db, request, key, status="error", code=exc.error_code)
        return _error_response(exc)
    _audit_access(db, request, key, status="ok", snapshot=True)
    return pool.ok(data)


def _text(value: Any) -> str | None:
    """把入参统一成字符串。

    OEP 是 ``body.get("x", "")`` 之后直接做字符串操作，数字 id 也照收；
    这里不更严（否则注册机传数字就被 422 挡在门外），真正的校验在 ``pool.validate_*``。
    """

    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _claim(
    request: Request, db: Session, payload: PoolClaimRequest, *, require_domain: bool
) -> JSONResponse | dict[str, Any]:
    settings = _settings(request)
    key: pool.PoolApiKey | None = None
    try:
        pool.ensure_pool_enabled(settings)
        key = pool.authenticate_pool_key(db, request)
        pool.enforce_rate_limit(request.app.state.pool_rate_limiter, key)
        caller_id = pool.validate_caller_id(_text(payload.caller_id))
        task_id = pool.validate_task_id(_text(payload.task_id))
        provider = pool.validate_provider(_text(payload.provider))
        project = pool.effective_project(key, pool.validate_project_key(_text(payload.project_key)))
        email_domain = pool.validate_email_domain(_text(payload.email_domain))
        pool.ensure_domain_allowed(key, email_domain)
        if require_domain and not email_domain:
            raise pool.PoolError("email_domain 不能为空", "email_domain_required", http_status=400)
        idempotency_key = (
            pool.validate_text(
                _text(payload.idempotency_key),
                field="idempotency_key",
                max_len=pool.IDEMPOTENCY_KEY_MAX_LEN,
            )
            or None
        )
        data = pool.claim_mailbox(
            db,
            settings,
            caller_id=caller_id,
            task_id=task_id,
            provider=provider,
            project=project,
            email_domain=email_domain,
            # 租约时长取服务端配置：OEP 的领取接口也不接受调用方指定 lease_seconds。
            lease_seconds=pool.validate_lease_seconds(None, settings=settings),
            allowed_domains=list(key.allowed_domains or []),
            idempotency_key=idempotency_key,
        )
    except pool.PoolError as exc:
        _audit_access(db, request, key, status="error", code=exc.error_code)
        return _error_response(exc)
    _audit_access(db, request, key, status="ok", account_id=data["account_id"], email=data["email"])
    return pool.ok(data)


@public_router.post("/claim-random")
def pool_claim_random(request: Request, payload: PoolClaimRequest, db: DbSession):
    """随机领取（可按 ``provider`` / Key 的域名白名单过滤）。"""

    return _claim(request, db, payload, require_domain=False)


@public_router.post("/claim-domain")
def pool_claim_domain(request: Request, payload: PoolClaimRequest, db: DbSession):
    """指定域名领取：``email_domain`` 必填（OEP 在控制器层就挡）。"""

    return _claim(request, db, payload, require_domain=True)


@public_router.post("/claim-release")
def pool_claim_release(request: Request, payload: PoolClaimActionRequest, db: DbSession):
    """释放领取：邮箱回池，不计数、不进冷却。"""

    settings = _settings(request)
    key: pool.PoolApiKey | None = None
    try:
        pool.ensure_pool_enabled(settings)
        key = pool.authenticate_pool_key(db, request)
        pool.enforce_rate_limit(request.app.state.pool_rate_limiter, key)
        account_id = pool.coerce_account_id(payload.account_id)
        status = pool.release_claim(
            db,
            settings,
            account_id=account_id,
            claim_token=pool.validate_claim_token(_text(payload.claim_token)),
            caller_id=pool.validate_caller_id(_text(payload.caller_id)),
            task_id=pool.validate_task_id(_text(payload.task_id)),
            project=pool.effective_project(key, pool.validate_project_key(_text(payload.project_key))),
            reason=pool.validate_text(_text(payload.reason), field="reason", max_len=pool.REASON_MAX_LEN),
        )
    except pool.PoolError as exc:
        _audit_access(db, request, key, status="error", code=exc.error_code)
        return _error_response(exc)
    _audit_access(db, request, key, status="ok", account_id=account_id)
    return pool.ok({"account_id": account_id, "pool_status": status})


@public_router.post("/claim-complete")
def pool_claim_complete(request: Request, payload: PoolClaimActionRequest, db: DbSession):
    """回传结果：按结果推进池状态（success → used，超时 → cooldown 等）。"""

    settings = _settings(request)
    key: pool.PoolApiKey | None = None
    try:
        pool.ensure_pool_enabled(settings)
        key = pool.authenticate_pool_key(db, request)
        pool.enforce_rate_limit(request.app.state.pool_rate_limiter, key)
        account_id = pool.coerce_account_id(payload.account_id)
        result = pool.validate_result(_text(payload.result))
        new_status = pool.complete_claim(
            db,
            settings,
            account_id=account_id,
            claim_token=pool.validate_claim_token(_text(payload.claim_token)),
            caller_id=pool.validate_caller_id(_text(payload.caller_id)),
            task_id=pool.validate_task_id(_text(payload.task_id)),
            project=pool.effective_project(key, pool.validate_project_key(_text(payload.project_key))),
            result=result,
            detail=pool.validate_text(_text(payload.detail), field="detail", max_len=pool.DETAIL_MAX_LEN),
        )
    except pool.PoolError as exc:
        _audit_access(db, request, key, status="error", code=exc.error_code)
        return _error_response(exc)
    _audit_access(db, request, key, status="ok", account_id=account_id, result=result)
    return pool.ok({"account_id": account_id, "pool_status": new_status})


# ================================================================== 运维：池密钥


class PoolKeyInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    project: str = Field(default="", max_length=128)
    allowed_domains: list[str] = Field(default_factory=list)
    rate_limit_per_minute: int | None = Field(default=None, ge=0, le=1_000_000)
    enabled: bool = True


class PoolKeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    project: str | None = Field(default=None, max_length=128)
    allowed_domains: list[str] | None = None
    rate_limit_per_minute: int | None = Field(default=None, ge=0, le=1_000_000)
    enabled: bool | None = None


class PoolMailboxInput(BaseModel):
    pool_status: str
    reason: str = Field(default="", max_length=256)


def _key_payload(row: PoolApiKey) -> dict:
    """对外形状：**不含** key_hash——摘要也没必要外露，免得被谁当凭据用。"""

    return {
        "id": row.id,
        "name": row.name,
        "project": row.project,
        "enabled": row.enabled,
        "allowed_domains": list(row.allowed_domains or []),
        "rate_limit_per_minute": row.rate_limit_per_minute,
        "created_at": row.created_at,
    }


@admin_router.post("/keys", status_code=201)
def create_pool_key(payload: PoolKeyInput, request: Request, db: DbSession, _: Operator):
    row, raw = pool.create_pool_key(
        db,
        name=payload.name,
        project=payload.project,
        allowed_domains=payload.allowed_domains,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        enabled=payload.enabled,
        settings=_settings(request),
    )
    pool.audit(
        db,
        event_type=pool.EVENT_KEY_CREATED,
        details={"key_id": row.id, "name": row.name, "project": row.project, "rate_limit_per_minute": row.rate_limit_per_minute},
    )
    db.commit()
    # 明文只在这个响应里出现一次，之后任何接口都拿不回来（库里只有摘要）。
    return {**_key_payload(row), "api_key": raw}


@admin_router.get("/keys")
def list_pool_keys(db: DbSession, _: Operator):
    rows = db.scalars(select(PoolApiKey).order_by(PoolApiKey.created_at)).all()
    return {"items": [_key_payload(row) for row in rows]}


@admin_router.patch("/keys/{key_id}")
def update_pool_key(key_id: str, payload: PoolKeyPatch, request: Request, db: DbSession, _: Operator):
    row = db.get(PoolApiKey, key_id)
    if row is None:
        raise HTTPException(404, {"code": "POOL_KEY_NOT_FOUND"})
    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.project is not None:
        row.project = payload.project.strip()
    if payload.allowed_domains is not None:
        row.allowed_domains = sorted({d.strip().lower() for d in payload.allowed_domains if d.strip()})
    if payload.rate_limit_per_minute is not None:
        row.rate_limit_per_minute = payload.rate_limit_per_minute
    if payload.enabled is not None:
        row.enabled = payload.enabled
    pool.audit(db, event_type=pool.EVENT_KEY_UPDATED, details={"key_id": row.id, "enabled": row.enabled, "changes": payload.model_dump(exclude_none=True, exclude={"allowed_domains"})})
    db.commit()
    return _key_payload(row)


# ================================================================== 运维：单邮箱接管


def _release_active_claims(db: Session, mailbox: InventoryMailbox, now, *, reason: str) -> int:
    """结束该邮箱上还挂着的租约，返回释放条数。"""

    claims = db.scalars(
        select(PoolClaim).where(PoolClaim.mailbox_id == mailbox.id, PoolClaim.status == pool.CLAIM_CLAIMED)
    ).all()
    for claim in claims:
        claim.status = pool.CLAIM_RELEASED
        claim.completed_at = now
        claim.result_detail = reason[: pool.REASON_MAX_LEN]
    return len(claims)


@admin_router.patch("/mailboxes/{mailbox_id}")
def set_mailbox_pool_status(mailbox_id: str, payload: PoolMailboxInput, db: DbSession, _: Operator):
    """运维手工接管某个邮箱的池状态。

    只开放 ``available`` / ``frozen`` / ``retired``：其余状态由流程推进，
    手工置 ``claimed`` 只会和租约打架。放回 ``available`` 时会把这个邮箱上还挂着的
    租约结束掉，否则它会一直显示 claimed 而谁也领不到。
    """

    target = payload.pool_status.strip().lower()
    if target not in pool.OPS_SETTABLE_STATUSES:
        raise HTTPException(
            422,
            {"code": "POOL_MAILBOX_STATUS_UNSUPPORTED", "allowed": list(pool.OPS_SETTABLE_STATUSES)},
        )
    mailbox = db.get(InventoryMailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(404, {"code": "POOL_MAILBOX_NOT_FOUND"})

    now = utc_now()
    released: int | None = None
    if target == pool.POOL_AVAILABLE:
        released = _release_active_claims(db, mailbox, now, reason=payload.reason or "ops_reset")
        mailbox.pool_lease_expires_at = None
        mailbox.pool_cooldown_until = None
    mailbox.pool_status = target
    pool.audit(
        db,
        event_type=pool.EVENT_MAILBOX_STATUS,
        mailbox_id=mailbox.id,
        details={"pool_status": target, "reason": payload.reason, "released_claims": released},
    )
    db.commit()
    return {"id": mailbox.id, "pool_status": mailbox.pool_status, "released_claims": released}
