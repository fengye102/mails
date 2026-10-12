"""E4：对外读信 / 取码（沿用 OEP 契约）。

对外路由的**所有**响应都走 ``pool.ok`` / ``pool.fail`` 扁平信封（与 OEP 的
``external_api_service.ok/fail`` 逐字段一致：``{success, code, message, data}``），
包括错误——注册机按顶层的 ``code`` 分支，不能把 FastAPI 的 ``{"detail": ...}`` 漏出去。

路径说明：
- ``/pool-api/v1/*``：平台原生前缀（E 规格命名）。
- ``/api/external/*``：OEP 兼容别名（池在 ``/pool/*``，读信在根下）。注册机若按 OEP
  全路径配 base_url，打的正是这些路径；handler 复用，不复制逻辑。

``wait-message`` 只实现了 sync 模式（async 探测目前如实返回 501，不假实现）。
"""

from __future__ import annotations

import time as _time
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from delivery_api import pool
from delivery_api.db import get_db
from delivery_api.models import DeliveryMessage, InventoryMailbox, PoolProbe, utc_now
from delivery_api.routes.pickups import _refresh_from_engine
from delivery_api.routes.pool import (
    pool_api_stats,
    pool_claim_complete,
    pool_claim_domain,
    pool_claim_random,
    pool_claim_release,
)
from delivery_api.settings import Settings
from delivery_api.time_utils import as_utc

verify_router = APIRouter(prefix="/pool-api/v1")
compat_router = APIRouter(prefix="/api/external")

DbSession = Annotated[Session, Depends(get_db)]

# 读信最多翻多少封来找验证码：够看到刚到的码，又不会把整箱拉出来。
VERIFY_SCAN_LIMIT = 10
WAIT_BASELINE_GRACE_SECONDS = 2


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _error_response(exc: pool.PoolError) -> JSONResponse:
    return JSONResponse(pool.fail(exc.error_code, str(exc), data=exc.data), status_code=exc.http_status)


def _mailbox_not_found(email: str) -> pool.PoolError:
    return pool.PoolError("未找到匹配邮箱", "EMAIL_NOT_FOUND", http_status=404, data=f"email={email}")


def _resolve_mailbox(db: Session, email: str) -> InventoryMailbox:
    address = (email or "").strip()
    if not address:
        raise pool.PoolError("email 不能为空", "EMAIL_NOT_FOUND", http_status=404)
    row = db.scalar(
        select(InventoryMailbox).where(func.lower(InventoryMailbox.address) == address.lower())
    )
    if row is None:
        raise _mailbox_not_found(address)
    return row


def _authed(db: Session, request: Request) -> pool.PoolApiKey:
    """统一入口：池开关 → API Key 鉴权 → 限流；失败抛 PoolError 由上层收。"""
    settings = _settings(request)
    pool.ensure_pool_enabled(settings)
    key = pool.authenticate_pool_key(db, request)
    pool.enforce_rate_limit(request.app.state.pool_rate_limiter, key)
    return key


def _audit_access(db: Session, request: Request, key: pool.PoolApiKey | None, *, status: str, code: str = "OK", **details) -> None:
    if key is None:
        return
    pool.audit(
        db,
        event_type=pool.EVENT_API_ACCESS,
        details={"endpoint": request.url.path, "status": status, "code": pool.public_code(code), "key_id": key.id, "key_name": key.name, **details},
    )
    db.commit()


def _plain(value: str | None) -> str | None:
    """取件/取码口径：绝不给取件人 HTML。"""
    return (value or "").strip() or None


def _row_to_item(row: DeliveryMessage) -> dict[str, Any]:
    return {
        "id": row.id,
        "email_address": row.recipient or "",
        "subject": row.subject or "",
        "from": row.sender or "",
        "sender": row.sender or "",
        "received_at": row.received_at.isoformat(),
        "body_text": _plain(row.body_text),
        "verification_code": row.otp_code,
        "verification_link": row.link,
        "link": row.link,
    }


def _message_filters(
    *,
    since_minutes: int | None,
    from_contains: str | None,
    subject_contains: str | None,
) -> list[Any]:
    filters: list[Any] = []
    if since_minutes is not None and since_minutes > 0:
        filters.append(DeliveryMessage.received_at >= utc_now() - timedelta(minutes=since_minutes))
    from_contains = (from_contains or "").strip()
    if from_contains:
        filters.append(DeliveryMessage.sender.ilike(f"%{from_contains}%"))
    subject_contains = (subject_contains or "").strip()
    if subject_contains:
        filters.append(DeliveryMessage.subject.ilike(f"%{subject_contains}%"))
    return filters


def _query_messages(
    db: Session,
    mailbox_id: str,
    *,
    top: int,
    skip: int,
    since_minutes: int | None = None,
    from_contains: str | None = None,
    subject_contains: str | None = None,
) -> tuple[list[DeliveryMessage], int]:
    filters = [DeliveryMessage.mailbox_id == mailbox_id]
    filters.extend(_message_filters(since_minutes=since_minutes, from_contains=from_contains, subject_contains=subject_contains))
    total = int(db.scalar(select(func.count()).select_from(DeliveryMessage).where(*filters)) or 0)
    rows = list(
        db.scalars(
            select(DeliveryMessage)
            .where(*filters)
            .order_by(DeliveryMessage.received_at.desc(), DeliveryMessage.id.desc())
            .offset(skip)
            .limit(top)
        ).all()
    )
    return rows, total


def _refresh(request: Request, db: Session, mailbox: InventoryMailbox) -> dict:
    """读前兜底刷一次上游；失败不炸（状态随 sync 如实回报）。"""
    try:
        return _refresh_from_engine(request, db, mailbox)
    except Exception:  # noqa: BLE001 - 取码不能因为刷新失败就挂
        return {"status": "unavailable", "code": "REFRESH_FAILED"}


def _verification_scan(db: Session, mailbox_id: str, *, field: str) -> DeliveryMessage | None:
    """最近一批里找带验证码/链接的消息。field: code | link | any。"""
    rows = _query_messages(db, mailbox_id, top=VERIFY_SCAN_LIMIT, skip=0)[0]
    for row in rows:
        if field == "code" and row.otp_code:
            return row
        if field == "link" and row.link:
            return row
        if field == "any" and (row.otp_code or row.link):
            return row
    return None


def _verification_result(row: DeliveryMessage, *, field: str, email: str) -> dict[str, Any]:
    code = row.otp_code
    link = row.link
    if field == "link":
        code = None
    elif field == "code":
        link = None
    formatted = code or link
    return {
        "verification_code": code,
        "verification_link": link,
        "formatted": formatted,
        "email": email,
        "matched_email_id": row.id,
        "from": row.sender or "",
        "subject": row.subject or "",
        "received_at": row.received_at.isoformat(),
        "method": "pool_archive",
    }


def _read_common(
    request: Request,
    db: Session,
    *,
    email: str,
    top: int,
    skip: int,
    folder: str,
    since_minutes: int | None,
    from_contains: str | None,
    subject_contains: str | None,
) -> tuple[pool.PoolApiKey, InventoryMailbox, dict[str, Any]]:
    """鉴权 + 解析邮箱 + 兜底刷新；返回 (key, mailbox, refresh_status)。"""
    key = _authed(db, request)
    mailbox = _resolve_mailbox(db, email)
    refresh = _refresh(request, db, mailbox)
    return key, mailbox, refresh


# ================================================================ /messages


@verify_router.get("/messages")
def pool_api_messages(
    request: Request,
    db: DbSession,
    email: str = Query(default=""),
    top: int = Query(default=20, ge=1, le=50),
    skip: int = Query(default=0, ge=0),
    folder: str = Query(default="inbox"),
    since_minutes: int | None = Query(default=None, ge=1),
    from_contains: str = Query(default=""),
    subject_contains: str = Query(default=""),
):
    key: pool.PoolApiKey | None = None
    try:
        key, mailbox, _refresh_status = _read_common(
            request, db,
            email=email, top=top, skip=skip, folder=folder,
            since_minutes=since_minutes, from_contains=from_contains, subject_contains=subject_contains,
        )
        rows, total = _query_messages(
            db, mailbox.id, top=top, skip=skip,
            since_minutes=since_minutes, from_contains=from_contains, subject_contains=subject_contains,
        )
    except pool.PoolError as exc:
        _audit_access(db, request, key, status="error", code=exc.error_code)
        return _error_response(exc)
    items = [_row_to_item(row) for row in rows]
    _audit_access(db, request, key, status="ok", mailbox_id=mailbox.id, total=total)
    return pool.ok({"emails": items, "count": total, "has_more": (skip + len(items)) < total})


# ============================================================ verification

@verify_router.get("/verification-code")
def pool_api_verification_code(
    request: Request,
    db: DbSession,
    email: str = Query(default=""),
    field: str = Query(default="code"),
    code_length: int | None = Query(default=None, ge=1, le=64),
    code_regex: str = Query(default=""),
    code_source: str = Query(default="all"),
    folder: str = Query(default="inbox"),
    from_contains: str = Query(default=""),
    subject_contains: str = Query(default=""),
    since_minutes: int | None = Query(default=10, ge=1),
):
    return _verification(request, db, field="code", email=email, folder=folder,
                         from_contains=from_contains, subject_contains=subject_contains, since_minutes=since_minutes)


@verify_router.get("/verification-link")
def pool_api_verification_link(
    request: Request,
    db: DbSession,
    email: str = Query(default=""),
    field: str = Query(default="link"),
    code_length: int | None = Query(default=None, ge=1, le=64),
    code_regex: str = Query(default=""),
    code_source: str = Query(default="all"),
    folder: str = Query(default="inbox"),
    from_contains: str = Query(default=""),
    subject_contains: str = Query(default=""),
    since_minutes: int | None = Query(default=10, ge=1),
):
    return _verification(request, db, field="link", email=email, folder=folder,
                         from_contains=from_contains, subject_contains=subject_contains, since_minutes=since_minutes)


def _verification(
    request: Request,
    db: Session,
    *,
    field: str,
    email: str,
    folder: str,
    from_contains: str,
    subject_contains: str,
    since_minutes: int | None,
):
    key: pool.PoolApiKey | None = None
    try:
        key, mailbox, _refresh_status = _read_common(
            request, db,
            email=email, top=VERIFY_SCAN_LIMIT, skip=0, folder=folder,
            since_minutes=since_minutes, from_contains=from_contains, subject_contains=subject_contains,
        )
        row = _verification_scan(db, mailbox.id, field=field)
    except pool.PoolError as exc:
        _audit_access(db, request, key, status="error", code=exc.error_code)
        return _error_response(exc)
    if row is None:
        err = pool.PoolError("未找到验证信息", "EMAIL_NOT_FOUND", http_status=404, data=f"email={email}")
        _audit_access(db, request, key, status="error", code=err.error_code)
        return _error_response(err)
    _audit_access(db, request, key, status="ok", mailbox_id=mailbox.id, matched_email_id=row.id)
    return pool.ok(_verification_result(row, field=field, email=mailbox.address), message="提取成功")


# ============================================================ wait-message

@verify_router.get("/wait-message")
def pool_api_wait_message(
    request: Request,
    db: DbSession,
    email: str = Query(default=""),
    timeout_seconds: int = Query(default=30, ge=1, le=300),
    poll_interval: float = Query(default=5, ge=0.2, le=60),
    mode: str = Query(default="sync"),
    folder: str = Query(default="inbox"),
    from_contains: str = Query(default=""),
    subject_contains: str = Query(default=""),
    since_minutes: int | None = Query(default=None, ge=1),
):
    key: pool.PoolApiKey | None = None
    try:
        key = _authed(db, request)
        mailbox = _resolve_mailbox(db, email)
        is_async = (mode or "sync").strip().lower() != "sync"
    except pool.PoolError as exc:
        _audit_access(db, request, key, status="error", code=exc.error_code)
        return _error_response(exc)

    if is_async:
        probe = PoolProbe(
            mailbox_id=mailbox.id,
            email=mailbox.address,
            status="pending",
            timeout_seconds=max(1, int(timeout_seconds)),
        )
        db.add(probe)
        db.commit()
        _audit_access(db, request, key, status="ok", mailbox_id=mailbox.id, probe_id=probe.id)
        return JSONResponse(pool.ok({"probe_id": probe.id}, message="已创建异步探测"), status_code=202)

    baseline = utc_now() - timedelta(seconds=WAIT_BASELINE_GRACE_SECONDS)
    deadline = _time.monotonic() + max(1, int(timeout_seconds))
    while _time.monotonic() < deadline:
        _refresh(request, db, mailbox)
        filters = [DeliveryMessage.received_at >= baseline]
        filters.extend(_message_filters(since_minutes=since_minutes, from_contains=from_contains, subject_contains=subject_contains))
        row = db.scalar(
            select(DeliveryMessage)
            .where(DeliveryMessage.mailbox_id == mailbox.id, *filters)
            .order_by(DeliveryMessage.received_at.desc(), DeliveryMessage.id.desc())
            .limit(1)
        )
        if row is not None:
            _audit_access(db, request, key, status="ok", mailbox_id=mailbox.id, matched_email_id=row.id)
            return pool.ok(_row_to_item(row), message="等待到新邮件")
        _time.sleep(poll_interval)

    err = pool.PoolError("等待邮件超时", "EMAIL_NOT_FOUND", http_status=404, data=f"email={email}")
    _audit_access(db, request, key, status="error", code=err.error_code)
    return _error_response(err)


# ================================================================ probe

@verify_router.get("/probe/{probe_id}")
def pool_api_get_probe(
    probe_id: str,
    request: Request,
    db: DbSession,
):
    key: pool.PoolApiKey | None = None
    try:
        key = _authed(db, request)
        probe = db.get(PoolProbe, probe_id)
        if probe is None:
            raise pool.PoolError("探测不存在", "PROBE_NOT_FOUND", http_status=404)
    except pool.PoolError as exc:
        _audit_access(db, request, key, status="error", code=exc.error_code)
        return _error_response(exc)

    # 已解决：直接回结果，不再碰上游。
    if probe.status == "found":
        _audit_access(db, request, key, status="ok", mailbox_id=probe.mailbox_id, probe_id=probe.id)
        return pool.ok({"probe_id": probe.id, "status": "found", "message": probe.result_json or {}}, message="已取到邮件")
    if probe.status == "timeout":
        err = pool.PoolError("等待邮件超时", "EMAIL_NOT_FOUND", http_status=404, data=f"email={probe.email}")
        _audit_access(db, request, key, status="error", code=err.error_code, probe_id=probe.id)
        return _error_response(err)

    # 轮询推进：每次 GET 顺手刷新一次上游并找「自探测创建以来」的新邮件。
    mailbox = db.get(InventoryMailbox, probe.mailbox_id)
    if mailbox is not None:
        _refresh(request, db, mailbox)
    baseline = (as_utc(probe.created_at) or utc_now()) - timedelta(seconds=WAIT_BASELINE_GRACE_SECONDS)
    row = db.scalar(
        select(DeliveryMessage)
        .where(DeliveryMessage.mailbox_id == probe.mailbox_id, DeliveryMessage.received_at >= baseline)
        .order_by(DeliveryMessage.received_at.desc(), DeliveryMessage.id.desc())
        .limit(1)
    )
    now = utc_now()
    if row is not None:
        probe.status = "found"
        probe.result_json = _row_to_item(row)
        probe.resolved_at = now
        db.commit()
        _audit_access(db, request, key, status="ok", mailbox_id=probe.mailbox_id, probe_id=probe.id, matched_email_id=row.id)
        return pool.ok({"probe_id": probe.id, "status": "found", "message": probe.result_json}, message="已取到邮件")
    if now >= baseline + timedelta(seconds=probe.timeout_seconds):
        probe.status = "timeout"
        probe.resolved_at = now
        db.commit()
        err = pool.PoolError("等待邮件超时", "EMAIL_NOT_FOUND", http_status=404, data=f"email={probe.email}")
        _audit_access(db, request, key, status="error", code=err.error_code, probe_id=probe.id)
        return _error_response(err)
    return pool.ok({"probe_id": probe.id, "status": "pending"})


# ========================================================= OEP 兼容别名

compat_router.add_api_route("/pool/claim-random", pool_claim_random, methods=["POST"])
compat_router.add_api_route("/pool/claim-domain", pool_claim_domain, methods=["POST"])
compat_router.add_api_route("/pool/claim-release", pool_claim_release, methods=["POST"])
compat_router.add_api_route("/pool/claim-complete", pool_claim_complete, methods=["POST"])
compat_router.add_api_route("/pool/stats", pool_api_stats, methods=["GET"])
compat_router.add_api_route("/messages", pool_api_messages, methods=["GET"])
compat_router.add_api_route("/verification-code", pool_api_verification_code, methods=["GET"])
compat_router.add_api_route("/verification-link", pool_api_verification_link, methods=["GET"])
compat_router.add_api_route("/wait-message", pool_api_wait_message, methods=["GET"])
compat_router.add_api_route("/probe/{probe_id}", pool_api_get_probe, methods=["GET"])