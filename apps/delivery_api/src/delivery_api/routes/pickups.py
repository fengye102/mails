import secrets
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from delivery_api.auth import require_operator
from delivery_api.credentials import MAX_CREDENTIAL_FAILURES, issue_credential
from delivery_api.db import get_db
from delivery_api.messages import count_messages, last_ingested_at, list_messages
from delivery_api.models import (
    DeliveryAllocation,
    DeliveryAuditEvent,
    InventoryMailbox,
    PickupAttempt,
    PickupCredential,
    PickupSession,
    utc_now,
)
from delivery_api.security import hash_secret, plain_text, valid_pickup_key
from delivery_api.time_utils import as_utc

router = APIRouter(prefix="/delivery-api/v1/pickups")


class PickupInput(BaseModel):
    allocation_id: str
    mode: str = Field(default="code", pattern="^(code|key)$")
    reissue: bool = False
    ttl_seconds: int | None = Field(default=None, ge=1, le=86400)


class VerifyInput(BaseModel):
    # 二选一：一次性取件码，或可重复验证的取件密钥；都不给由路由层回固定错误码。
    pickup_code: str | None = Field(default=None, max_length=128)
    pickup_key: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=320)


def pickup_bucket_keys(source: str, value: str) -> list[str]:
    """反枚举双桶：提交值摘要桶（防跨 IP 爆破同一凭证）+ 来源 IP 桶（防单来源扫描）。"""
    return [
        hash_secret(f"pickup-value:{hash_secret(value)}"),
        hash_secret(f"pickup-source:{source}"),
    ]


def _source_of(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _load_bucket(db: Session, bucket_hash: str, now: datetime, window: timedelta) -> PickupAttempt:
    attempt = db.scalar(
        select(PickupAttempt)
        .where(PickupAttempt.source_hash == bucket_hash)
        .with_for_update()
    )
    if attempt is None:
        attempt = PickupAttempt(source_hash=bucket_hash, window_started_at=now, failed_attempts=0)
        db.add(attempt)
        db.flush()
        return attempt
    started = as_utc(attempt.window_started_at)
    if started is None or started + window <= now:
        attempt.window_started_at = now
        attempt.failed_attempts = 0
        attempt.blocked_until = None
    return attempt


def _is_blocked(attempt: PickupAttempt, now: datetime) -> bool:
    blocked_until = as_utc(attempt.blocked_until)
    return blocked_until is not None and blocked_until > now


def _record_pickup_failure(
    db: Session, request: Request, now: datetime, value: str
) -> bool:
    """记一次失败；返回 True 表示本次请求应当被 429 拒绝（已有桶处于封禁窗口）。

    只落摘要，不落取件码/密钥明文；失败详情里也只有桶摘要。
    """
    settings = request.app.state.settings
    window = timedelta(seconds=settings.pickup_failure_window_seconds)
    buckets = [
        _load_bucket(db, bucket_hash, now, window)
        for bucket_hash in pickup_bucket_keys(_source_of(request), value)
    ]
    if any(_is_blocked(bucket, now) for bucket in buckets):
        db.commit()
        return True
    for bucket in buckets:
        bucket.failed_attempts += 1
        if bucket.failed_attempts >= settings.pickup_failure_limit:
            bucket.blocked_until = now + timedelta(seconds=settings.pickup_backoff_seconds)
    db.add(
        DeliveryAuditEvent(
            event_type="pickup_verification_failed",
            details={"buckets": [bucket.source_hash for bucket in buckets]},
        )
    )
    db.commit()
    return False


def _clear_pickup_failures(db: Session, request: Request, value: str) -> None:
    """成功取件后清空两个桶，避免正常用户被自己之前的误输连坐。"""
    now = utc_now()
    for bucket_hash in pickup_bucket_keys(_source_of(request), value):
        bucket = db.scalar(
            select(PickupAttempt)
            .where(PickupAttempt.source_hash == bucket_hash)
            .with_for_update()
        )
        if bucket is None:
            continue
        bucket.failed_attempts = 0
        bucket.blocked_until = None
        bucket.window_started_at = now
    db.commit()


def _find_credential(
    db: Session, payload: VerifyInput, value: str
) -> PickupCredential | None:
    if payload.pickup_key:
        if not valid_pickup_key(value):
            return None
        return db.scalar(
            select(PickupCredential)
            .where(PickupCredential.key_hash == hash_secret(value))
            .with_for_update()
        )
    return db.scalar(
        select(PickupCredential)
        .where(PickupCredential.code_hash == hash_secret(value))
        .with_for_update()
    )


def _credential_usable(credential: PickupCredential | None, now: datetime, key_mode: bool) -> bool:
    if credential is None:
        return False
    if credential.failed_attempts >= MAX_CREDENTIAL_FAILURES:
        return False
    if as_utc(credential.expires_at) <= now:
        return False
    if key_mode:
        return bool(credential.key_hash)
    return credential.consumed_at is None


@router.post("", status_code=201, dependencies=[Depends(require_operator)])
def create_pickup(
    payload: PickupInput,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """签发取件凭证：`mode=code` 一次性取件码，`mode=key` 可重复使用的取件密钥。

    明文只出现在本次响应里；`reissue=true` 会让该分配下旧的码/密钥立刻失效。
    """
    allocation = db.get(DeliveryAllocation, payload.allocation_id)
    now = utc_now()
    if allocation is None or allocation.status != "active" or as_utc(allocation.expires_at) <= now:
        raise HTTPException(409, {"code": "DELIVERY_ALLOCATION_EXPIRED"})
    mailbox = db.get(InventoryMailbox, allocation.mailbox_id)
    if mailbox is None or mailbox.status != "allocated":
        raise HTTPException(409, {"code": "DELIVERY_ALLOCATION_EXPIRED"})
    settings = request.app.state.settings
    return issue_credential(
        db,
        allocation,
        mailbox.address,
        mode=payload.mode,
        ttl_seconds=payload.ttl_seconds or settings.pickup_ttl_seconds,
        now=now,
        pickup_base_url=settings.pickup_base_url,
        reissue=payload.reissue,
    )


@router.post("/verify")
def verify_pickup(
    payload: VerifyInput,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    value = (payload.pickup_key or payload.pickup_code or "").strip()
    now = utc_now()
    if not value:
        raise HTTPException(400, {"code": "DELIVERY_PICKUP_CREDENTIAL_REQUIRED"})
    key_mode = bool(payload.pickup_key)

    def failure() -> HTTPException:
        if _record_pickup_failure(db, request, now, value):
            return HTTPException(429, {"code": "DELIVERY_PICKUP_RETRY_LATER"})
        return HTTPException(401, {"code": "DELIVERY_PICKUP_INVALID"})

    credential = _find_credential(db, payload, value)
    if not _credential_usable(credential, now, key_mode):
        raise failure()
    assert credential is not None  # _credential_usable 已排除 None

    allocation = db.get(DeliveryAllocation, credential.allocation_id)
    mailbox = db.get(InventoryMailbox, allocation.mailbox_id) if allocation else None
    scope_valid = (
        allocation is not None
        and mailbox is not None
        and mailbox.status == "allocated"
        and allocation.status == "active"
        and as_utc(allocation.expires_at) > now
    )
    address_matches = (
        not payload.email
        or payload.email.strip().lower() == (mailbox.address or "").strip().lower()
    )
    if not scope_valid or not address_matches:
        # 已知凭证但分配失效/地址不符：记在这张凭证头上（旧实现只查不增，上限形同虚设）。
        credential.failed_attempts += 1
        db.commit()
        raise failure()
    assert allocation is not None and mailbox is not None  # scope_valid 已保证

    token = secrets.token_urlsafe(32)
    session_expires_at = min(
        as_utc(credential.expires_at),
        now + timedelta(seconds=request.app.state.settings.session_ttl_seconds),
    )
    if not key_mode:
        # 取件码一次性：用带条件的 UPDATE 防并发重放。
        consumed = db.execute(
            update(PickupCredential)
            .where(
                PickupCredential.id == credential.id,
                PickupCredential.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
        if consumed.rowcount != 1:
            db.rollback()
            raise failure()
    db.add(
        PickupSession(
            credential_id=credential.id,
            allocation_id=allocation.id,
            token_hash=hash_secret(token),
            expires_at=session_expires_at,
        )
    )
    db.add(
        DeliveryAuditEvent(
            event_type="pickup_verified",
            allocation_id=allocation.id,
            mailbox_id=allocation.mailbox_id,
            details={"mode": "key" if key_mode else "code"},
        )
    )
    db.commit()
    _clear_pickup_failures(db, request, value)
    return {
        "pickup_id": credential.id,
        # 取件人手里只有取件码/密钥：读邮箱所需的分配标识
        # 必须随验证结果一并返回，否则前端无法读取邮箱。
        "allocation_id": allocation.id,
        "session_token": token,
        "expires_at": session_expires_at.isoformat(),
    }


@router.get("/{allocation_id}/mailbox")
def read_mailbox(
    allocation_id: str,
    authorization: Annotated[str | None, Header()] = None,
    db: Annotated[Session, Depends(get_db)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    token = authorization.removeprefix("Bearer ") if authorization else ""
    now = utc_now()
    session = db.scalar(
        select(PickupSession).where(PickupSession.token_hash == hash_secret(token))
    )
    if (
        session is None
        or as_utc(session.expires_at) is None
        or as_utc(session.expires_at) <= now
    ):
        raise HTTPException(401, {"code": "DELIVERY_PICKUP_SESSION_INVALID"})
    allocation = db.get(DeliveryAllocation, allocation_id)
    if (
        allocation is None
        or allocation.id != session.allocation_id
        or allocation.status != "active"
        or as_utc(allocation.expires_at) is None
        or as_utc(allocation.expires_at) <= now
    ):
        raise HTTPException(403, {"code": "DELIVERY_PICKUP_SCOPE_INVALID"})
    mailbox = db.get(InventoryMailbox, allocation.mailbox_id)
    if mailbox is None:
        raise HTTPException(404, {"code": "DELIVERY_MAILBOX_NOT_FOUND"})
    if mailbox.status != "allocated":
        raise HTTPException(403, {"code": "DELIVERY_PICKUP_SCOPE_INVALID"})
    rows = list_messages(db, mailbox.id, limit)
    ingested_at = last_ingested_at(db, mailbox.id)
    messages = [
        {
            "id": row.id,
            "sender": row.sender,
            "subject": row.subject,
            "received_at": row.received_at.isoformat(),
            # 入库已转纯文本，这里再兜一次底：任何情况下都不把 HTML 交给取件人。
            "body_text": plain_text(row.body_text),
            "verification_code": row.otp_code,
            "link": row.link,
        }
        for row in rows
    ]
    return {
        "address": mailbox.address,
        # C 不做上游拉信（上游引擎属批次 7 范围）：只如实回报入库状态，不伪装成实时同步。
        "sync": {
            "messages_total": count_messages(db, mailbox.id),
            "last_ingested_at": ingested_at.isoformat() if ingested_at else None,
        },
        "messages": messages,
    }

