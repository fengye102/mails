from datetime import datetime, timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from delivery_api.auth import require_operator
from delivery_api.cleanup import cleanup_expired_allocations
from delivery_api.db import get_db
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
        messages=payload.messages,
        expires_at=payload.expires_at,
    )
    db.add(row)
    db.add(DeliveryAuditEvent(event_type="mailbox_registered", mailbox_id=row.id))
    db.commit()
    return {"id": row.id, "address": row.address, "status": row.status}


@router.post("/allocations", status_code=201, dependencies=[Depends(require_operator)])
def allocate(
    payload: AllocationInput,
    db: Annotated[Session, Depends(get_db)],
):
    row = db.scalar(
        select(InventoryMailbox)
        .where(InventoryMailbox.id == payload.mailbox_id)
        .with_for_update()
    )
    now = utc_now()
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

    allocation = DeliveryAllocation(
        id=str(uuid4()),
        mailbox_id=row.id,
        external_ref=payload.external_ref,
        allocated_at=now,
        expires_at=now + timedelta(seconds=payload.ttl_seconds),
        status="active",
    )
    row.status = "allocated"
    db.add(allocation)
    db.add(
        DeliveryAuditEvent(
            event_type="mailbox_allocated",
            mailbox_id=row.id,
            allocation_id=allocation.id,
        )
    )
    db.commit()
    return {
        "id": allocation.id,
        "mailbox_id": row.id,
        "status": allocation.status,
        "expires_at": allocation.expires_at.isoformat(),
    }


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
