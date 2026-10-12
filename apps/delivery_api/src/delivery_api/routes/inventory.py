from datetime import datetime, timedelta
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from delivery_api import pool
from delivery_api.auth import require_operator
from delivery_api.cleanup import cleanup_expired_allocations
from delivery_api.credentials import issue_credential
from delivery_api.db import get_db
from delivery_api.icloud_engine import EngineError, ICloudEngineClient
from delivery_api.inventory_sync import DEFAULT_SOURCE_LABEL, apply_alias_snapshot
from delivery_api.messages import (
    count_messages,
    ingest_messages,
    last_ingested_at,
    list_messages,
)
from delivery_api.models import (
    DeliveryAllocation,
    DeliveryAuditEvent,
    InventoryMailbox,
    utc_now,
)
from delivery_api.security import encrypt_value
from delivery_api.time_utils import as_utc

router = APIRouter(prefix="/delivery-api/v1/inventory")



class MailboxInput(BaseModel):
    address: str = Field(min_length=3, max_length=320)
    source_label: str = Field(min_length=1, max_length=64)
    access_token: str = ""
    messages: list[dict] = Field(default_factory=list)
    expires_at: datetime | None = None


class AllocationInput(BaseModel):
    mailbox_id: str
    external_ref: str = Field(min_length=1, max_length=256)
    ttl_seconds: int = Field(default=3600, ge=0, le=2592000)


class BatchAllocationInput(BaseModel):
    mailbox_ids: list[str] = Field(min_length=1, max_length=750)
    external_ref: str = Field(default="", max_length=256)
    ttl_seconds: int = Field(default=3600, ge=0, le=2592000)
    # 批量发货时通常直接要密钥；不需要就只建分配，之后再单独签发。
    issue_key: bool = False


def allocate_mailbox(
    db: Session, mailbox_id: str, external_ref: str, ttl_seconds: int, now: datetime
) -> DeliveryAllocation:
    """把一个可用邮箱分配给订单；不可用时抛固定错误码 409。"""
    row = db.scalar(
        select(InventoryMailbox).where(InventoryMailbox.id == mailbox_id).with_for_update()
    )
    if row is None:
        raise HTTPException(409, {"code": "DELIVERY_MAILBOX_NOT_AVAILABLE"})

    mailbox_expires_at = as_utc(row.expires_at)
    if mailbox_expires_at is not None and mailbox_expires_at <= now:
        row.status = "expired"

    if row.status == "allocated":
        active = db.scalar(
            select(DeliveryAllocation)
            .where(
                DeliveryAllocation.mailbox_id == row.id,
                DeliveryAllocation.status == "active",
            )
            .order_by(DeliveryAllocation.expires_at.desc())
            .with_for_update()
        )
        if active is not None and as_utc(active.expires_at) <= now:
            active.status = "expired"
            row.status = "available"
            db.add(
                DeliveryAuditEvent(
                    event_type="allocation_expired",
                    allocation_id=active.id,
                    mailbox_id=row.id,
                )
            )

    if row.status != "available":
        raise HTTPException(409, {"code": "DELIVERY_MAILBOX_NOT_AVAILABLE"})

    # 池里正被注册机租用的邮箱不许再发给取件订单：两条链路同时占用同一个地址会串号。
    # 反向由池侧把关（池只发库存 status == available 且未被领用的邮箱），方向对称。
    if row.pool_status == pool.POOL_CLAIMED:
        raise HTTPException(409, {"code": "DELIVERY_MAILBOX_POOL_CLAIMED"})

    allocation = DeliveryAllocation(
        id=str(uuid4()),
        mailbox_id=row.id,
        external_ref=external_ref,
        allocated_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        status="active",
    )
    row.status = "allocated"
    db.add(allocation)
    # 同上：审计事件引用新分配的 id，先 flush。
    db.flush()
    db.add(
        DeliveryAuditEvent(
            event_type="mailbox_allocated",
            mailbox_id=row.id,
            allocation_id=allocation.id,
        )
    )
    return allocation


class MessagePushInput(BaseModel):
    # 上游字段命名不一（id/message_id/uid、from/from_address…），入库层统一归一化，
    # 所以这里只限制条数，不做字段白名单，避免合法邮件被 422 挡在门外。
    messages: list[dict] = Field(min_length=1, max_length=200)


@router.post("/mailboxes", status_code=201, dependencies=[Depends(require_operator)])
def register_mailbox(
    payload: MailboxInput,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    token = (
        encrypt_value(request.app.state.settings.encryption_key, payload.access_token)
        if payload.access_token
        else b""
    )
    row = InventoryMailbox(
        id=str(uuid4()),
        address=payload.address,
        source_label=payload.source_label,
        access_token_ciphertext=token,
        expires_at=payload.expires_at,
    )
    db.add(row)
    # 审计事件外键指向本行，必须先 flush 让被引用行落库（SQLAlchemy 不保证按 add 顺序插入）。
    db.flush()
    db.add(DeliveryAuditEvent(event_type="mailbox_registered", mailbox_id=row.id))
    db.commit()
    # 登记时带来的历史邮件走同一套幂等入库，邮箱行上不再保留第二份 JSON 真相。
    outcome = ingest_messages(db, row.id, payload.messages) if payload.messages else None
    return {
        "id": row.id,
        "address": row.address,
        "status": row.status,
        "messages_total": outcome.total if outcome else 0,
    }


@router.post(
    "/mailboxes/{mailbox_id}/messages",
    dependencies=[Depends(require_operator)],
)
def push_mailbox_messages(
    mailbox_id: str,
    payload: MessagePushInput,
    db: Annotated[Session, Depends(get_db)],
):
    """幂等推送邮件；同一 provider_message_id 重复推送只更新内容。"""
    mailbox = db.get(InventoryMailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(404, {"code": "DELIVERY_MAILBOX_NOT_FOUND"})
    outcome = ingest_messages(db, mailbox.id, payload.messages)
    db.add(
        DeliveryAuditEvent(
            event_type="messages_ingested",
            mailbox_id=mailbox.id,
            details={
                "inserted": outcome.inserted,
                "updated": outcome.updated,
                "total": outcome.total,
            },
        )
    )
    db.commit()
    return outcome.as_dict()


@router.get(
    "/mailboxes/{mailbox_id}/messages",
    dependencies=[Depends(require_operator)],
)
def read_mailbox_messages(
    mailbox_id: str,
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    """运营侧核对用：与取件人看到的是同一批邮件。"""
    mailbox = db.get(InventoryMailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(404, {"code": "DELIVERY_MAILBOX_NOT_FOUND"})
    ingested_at = last_ingested_at(db, mailbox.id)
    return {
        "address": mailbox.address,
        "total": count_messages(db, mailbox.id),
        "last_ingested_at": ingested_at.isoformat() if ingested_at else None,
        "items": [
            {
                "id": row.id,
                "provider_message_id": row.provider_message_id,
                "sender": row.sender,
                "recipient": row.recipient,
                "subject": row.subject,
                "body_text": row.body_text,
                "otp_code": row.otp_code,
                "link": row.link,
                "received_at": row.received_at.isoformat(),
                "created_at": row.created_at.isoformat(),
            }
            for row in list_messages(db, mailbox.id, limit)
        ],
    }


class InventorySyncInput(BaseModel):
    """同步参数；留空即用服务端配置（引擎地址、账号、来源标签都在环境变量里）。"""

    account_id: str = Field(default="", max_length=256)
    source_label: str = Field(default="", max_length=64)


def get_engine_client(request: Request) -> httpx.Client:
    """引擎 HTTP 客户端（app 上的一条连接池，同步与取件刷新共用）。"""
    return request.app.state.icloud_engine_client


@router.post("/sync", dependencies=[Depends(require_operator)])
def sync_inventory(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[httpx.Client, Depends(get_engine_client)],
    payload: InventorySyncInput | None = None,
):
    """拉引擎的别名快照对齐库存（幂等、可重复执行）。

    上游有 → 建/恢复为 available；上游停用 → inactive；上游已不存在 → missing（不删）；
    上游已不存在但本地有有效分配 → 只计数不动行（设计规格不变量 6）。
    """
    settings = request.app.state.settings
    base_url = (settings.icloud_engine_url or "").strip()
    token = settings.icloud_engine_token or ""
    account_id = (payload.account_id if payload else "").strip() or (
        settings.icloud_engine_account_id or ""
    ).strip()
    source_label = (payload.source_label if payload else "").strip() or (
        settings.icloud_engine_source_label or DEFAULT_SOURCE_LABEL
    ).strip()
    if not base_url or not token or not account_id:
        # 没配置就说没配置：不能让「同步成功 0 条」掩盖「其实根本没连上引擎」。
        raise HTTPException(409, {"code": "DELIVERY_ENGINE_NOT_CONFIGURED"})

    engine = ICloudEngineClient(client, base_url, token)
    try:
        snapshots = engine.list_aliases(account_id)
    except EngineError as error:
        raise HTTPException(502, {"code": error.code}) from None

    outcome = apply_alias_snapshot(db, snapshots, source_label=source_label, now=utc_now())
    db.add(
        DeliveryAuditEvent(
            event_type="inventory_synced",
            details={
                "account_id": account_id,
                "source_label": source_label,
                **outcome.as_dict(),
            },
        )
    )
    db.commit()
    return {
        "account_id": account_id,
        "source_label": source_label,
        **outcome.as_dict(),
    }


@router.post("/allocations", status_code=201, dependencies=[Depends(require_operator)])
def allocate(
    payload: AllocationInput,
    db: Annotated[Session, Depends(get_db)],
):
    now = utc_now()
    allocation = allocate_mailbox(
        db, payload.mailbox_id, payload.external_ref, payload.ttl_seconds, now
    )
    db.commit()
    return {
        "id": allocation.id,
        "mailbox_id": allocation.mailbox_id,
        "status": allocation.status,
        "expires_at": allocation.expires_at.isoformat(),
    }


@router.post("/allocations/batch", status_code=201, dependencies=[Depends(require_operator)])
def allocate_batch(
    payload: BatchAllocationInput,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """批量分配（1..750）：去重、逐项失败、部分成功，并可按需直接签发取件密钥。"""
    settings = request.app.state.settings
    now = utc_now()
    multiple = len(payload.mailbox_ids) > 1
    seen: set[str] = set()
    deliveries: list[dict] = []
    failures: list[dict] = []
    for index, raw_id in enumerate(payload.mailbox_ids):
        mailbox_id = raw_id.strip()
        if not mailbox_id or mailbox_id in seen:
            if not mailbox_id:
                failures.append({"mailbox_id": raw_id, "code": "DELIVERY_MAILBOX_NOT_AVAILABLE"})
            continue
        seen.add(mailbox_id)
        external_ref = payload.external_ref.strip()
        if external_ref and multiple:
            external_ref = f"{external_ref}-{index + 1:03d}"
        try:
            allocation = allocate_mailbox(
                db, mailbox_id, external_ref, payload.ttl_seconds, now
            )
            mailbox = db.get(InventoryMailbox, allocation.mailbox_id)
            assert mailbox is not None  # allocate_mailbox 已确认存在
            db.commit()
        except HTTPException as error:
            db.rollback()
            detail = error.detail if isinstance(error.detail, dict) else {}
            failures.append(
                {
                    "mailbox_id": mailbox_id,
                    "code": detail.get("code", "DELIVERY_MAILBOX_NOT_AVAILABLE"),
                }
            )
            continue
        item: dict = {
            "allocation_id": allocation.id,
            "mailbox_id": allocation.mailbox_id,
            "address": mailbox.address,
            "external_ref": allocation.external_ref,
            "expires_at": allocation.expires_at.isoformat(),
        }
        if payload.issue_key:
            try:
                credential = issue_credential(
                    db,
                    allocation,
                    mailbox.address,
                    mode="key",
                    ttl_seconds=payload.ttl_seconds or settings.pickup_ttl_seconds,
                    now=now,
                    pickup_base_url=settings.pickup_base_url,
                )
            except HTTPException as error:
                db.rollback()
                detail = error.detail if isinstance(error.detail, dict) else {}
                failures.append(
                    {
                        "mailbox_id": mailbox_id,
                        "code": detail.get("code", "DELIVERY_PICKUP_INVALID"),
                    }
                )
                continue
            item["pickup_key"] = credential["pickup_key"]
            item["pickup_url"] = credential["pickup_url"]
            item["delivery_text"] = credential["delivery_text"]
            item["pickup_expires_at"] = credential["expires_at"]
        deliveries.append(item)
    if not deliveries:
        db.rollback()
        raise HTTPException(409, {"code": "DELIVERY_MAILBOX_NOT_AVAILABLE"})
    return {"count": len(deliveries), "deliveries": deliveries, "failures": failures}


@router.post("/allocations/cleanup", dependencies=[Depends(require_operator)])
def cleanup_allocations(db: Annotated[Session, Depends(get_db)]):
    return {"expired_allocations": cleanup_expired_allocations(db)}


@router.post(
    "/allocations/{allocation_id}/revoke",
    dependencies=[Depends(require_operator)],
)
def revoke_allocation(
    allocation_id: str,
    db: Annotated[Session, Depends(get_db)],
):
    allocation = db.get(DeliveryAllocation, allocation_id)
    if allocation is None:
        raise HTTPException(404, {"code": "DELIVERY_ALLOCATION_NOT_FOUND"})
    allocation.status = "revoked"
    mailbox = db.get(InventoryMailbox, allocation.mailbox_id)
    if mailbox is not None and mailbox.status == "allocated":
        mailbox.status = "available"
    db.add(
        DeliveryAuditEvent(
            event_type="allocation_revoked",
            allocation_id=allocation.id,
            mailbox_id=allocation.mailbox_id,
        )
    )
    db.commit()
    return {"id": allocation.id, "status": allocation.status}


@router.post(
    "/mailboxes/{mailbox_id}/disable",
    dependencies=[Depends(require_operator)],
)
def disable_mailbox(
    mailbox_id: str,
    db: Annotated[Session, Depends(get_db)],
):
    mailbox = db.get(InventoryMailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(404, {"code": "DELIVERY_MAILBOX_NOT_FOUND"})
    mailbox.status = "disabled"
    db.add(DeliveryAuditEvent(event_type="mailbox_disabled", mailbox_id=mailbox.id))
    db.commit()
    return {"id": mailbox.id, "status": mailbox.status}


@router.get("/audit", dependencies=[Depends(require_operator)])
def audit(db: Annotated[Session, Depends(get_db)]):
    events = db.scalars(
        select(DeliveryAuditEvent)
        .order_by(DeliveryAuditEvent.created_at.desc())
        .limit(100)
    ).all()
    return {
        "items": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "allocation_id": event.allocation_id,
                "mailbox_id": event.mailbox_id,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ]
    }
