import hmac
from datetime import timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from delivery_api.db import get_db
from delivery_api.models import (
    DeliveryAllocation,
    DeliveryAuditEvent,
    InventoryMailbox,
    utc_now,
)
from delivery_api.security import encrypt_value

router = APIRouter(prefix="/delivery-api/v1/inventory")

def _aware(value):
    return value.replace(tzinfo=utc_now().tzinfo) if value is not None and value.tzinfo is None else value

def operator(request: Request, authorization: Annotated[str | None, Header()] = None):
    expected = request.app.state.settings.operator_token
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, {"code": "DELIVERY_OPERATOR_REQUIRED"})

class MailboxInput(BaseModel):
    address: str = Field(min_length=3, max_length=320)
    source_label: str = Field(min_length=1, max_length=64)
    access_token: str = ""
    messages: list[dict] = Field(default_factory=list)
    expires_at: object | None = None

class AllocationInput(BaseModel):
    mailbox_id: str
    external_ref: str = Field(min_length=1, max_length=256)
    ttl_seconds: int = Field(default=3600, ge=0, le=2592000)

@router.post("/mailboxes", status_code=201, dependencies=[Depends(operator)])
def register_mailbox(payload: MailboxInput, request: Request, db: Annotated[Session, Depends(get_db)]):
    token = encrypt_value(request.app.state.settings.encryption_key, payload.access_token) if payload.access_token else b""
    row = InventoryMailbox(id=str(uuid4()), address=payload.address, source_label=payload.source_label, access_token_ciphertext=token, messages=payload.messages, expires_at=payload.expires_at)
    db.add(row); db.add(DeliveryAuditEvent(event_type="mailbox_registered", mailbox_id=row.id)); db.commit()
    return {"id": row.id, "address": row.address, "status": row.status}

@router.post("/allocations", status_code=201, dependencies=[Depends(operator)])
def allocate(payload: AllocationInput, db: Annotated[Session, Depends(get_db)]):
    row = db.scalar(select(InventoryMailbox).where(InventoryMailbox.id == payload.mailbox_id).with_for_update())
    now = utc_now()
    if row is None or row.status != "available" or (_aware(row.expires_at) and _aware(row.expires_at) <= now):
        raise HTTPException(409, {"code": "DELIVERY_MAILBOX_NOT_AVAILABLE"})
    allocation = DeliveryAllocation(id=str(uuid4()), mailbox_id=row.id, external_ref=payload.external_ref, allocated_at=now, expires_at=now + timedelta(seconds=payload.ttl_seconds), status="active")
    row.status = "allocated"; db.add(allocation); db.add(DeliveryAuditEvent(event_type="mailbox_allocated", mailbox_id=row.id, allocation_id=allocation.id)); db.commit()
    return {"id": allocation.id, "mailbox_id": row.id, "status": allocation.status, "expires_at": allocation.expires_at.isoformat()}
