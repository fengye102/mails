import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from delivery_api.auth import require_operator
from delivery_api.db import get_db
from delivery_api.models import (
    DeliveryAllocation,
    DeliveryAuditEvent,
    InventoryMailbox,
    PickupCredential,
    utc_now,
)
from delivery_api.security import hash_secret, new_code, plain_text
from delivery_api.time_utils import as_utc

router = APIRouter(prefix="/delivery-api/v1/pickups")


class PickupInput(BaseModel):
    allocation_id: str
    ttl_seconds: int | None = Field(default=None, ge=1, le=86400)


class VerifyInput(BaseModel):
    pickup_code: str = Field(min_length=1, max_length=128)


@router.post("", status_code=201, dependencies=[Depends(require_operator)])
def create_pickup(
    payload: PickupInput,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    allocation = db.get(DeliveryAllocation, payload.allocation_id)
    now = utc_now()
    if allocation is None or allocation.status != "active" or as_utc(allocation.expires_at) <= now:
        raise HTTPException(409, {"code": "DELIVERY_ALLOCATION_EXPIRED"})
    mailbox = db.get(InventoryMailbox, allocation.mailbox_id)
    if mailbox is None or mailbox.status != "allocated":
        raise HTTPException(409, {"code": "DELIVERY_ALLOCATION_EXPIRED"})
    code = new_code()
    ttl = payload.ttl_seconds or request.app.state.settings.pickup_ttl_seconds
    credential = PickupCredential(
        allocation_id=allocation.id,
        code_hash=hash_secret(code),
        expires_at=min(
            as_utc(allocation.expires_at), now + timedelta(seconds=ttl)
        ),
    )
    db.add(credential)
    db.add(
        DeliveryAuditEvent(
            event_type="pickup_code_created",
            allocation_id=allocation.id,
            mailbox_id=allocation.mailbox_id,
        )
    )
    db.commit()
    return {
        "pickup_id": credential.id,
        "pickup_code": code,
        "expires_at": credential.expires_at.isoformat(),
    }


@router.post("/verify")
def verify_pickup(
    payload: VerifyInput,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    credential = db.scalar(
        select(PickupCredential)
        .where(PickupCredential.code_hash == hash_secret(payload.pickup_code))
        .with_for_update()
    )
    now = utc_now()
    if (
        credential is None
        or credential.consumed_at is not None
        or as_utc(credential.expires_at) <= now
        or credential.failed_attempts >= 5
    ):
        raise HTTPException(401, {"code": "DELIVERY_PICKUP_INVALID"})
    allocation = db.get(DeliveryAllocation, credential.allocation_id)
    mailbox = db.get(InventoryMailbox, allocation.mailbox_id) if allocation else None
    if (
        allocation is None
        or mailbox is None
        or mailbox.status != "allocated"
        or allocation.status != "active"
        or as_utc(allocation.expires_at) <= now
    ):
        raise HTTPException(401, {"code": "DELIVERY_PICKUP_INVALID"})
    token = secrets.token_urlsafe(32)
    session_expires_at = min(
        as_utc(credential.expires_at),
        now + timedelta(seconds=request.app.state.settings.session_ttl_seconds),
    )
    result = db.execute(
        update(PickupCredential)
        .where(
            PickupCredential.id == credential.id,
            PickupCredential.consumed_at.is_(None),
        )
        .values(
            consumed_at=now,
            session_token_hash=hash_secret(token),
            session_expires_at=session_expires_at,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(401, {"code": "DELIVERY_PICKUP_INVALID"})
    db.add(
        DeliveryAuditEvent(
            event_type="pickup_verified",
            allocation_id=allocation.id,
            mailbox_id=allocation.mailbox_id,
        )
    )
    db.commit()
    return {
        "pickup_id": credential.id,
        "session_token": token,
        "expires_at": session_expires_at.isoformat(),
    }


@router.get("/{allocation_id}/mailbox")
def read_mailbox(
    allocation_id: str,
    authorization: Annotated[str | None, Header()] = None,
    db: Annotated[Session, Depends(get_db)] = None,
):
    token = authorization.removeprefix("Bearer ") if authorization else ""
    credential = db.scalar(
        select(PickupCredential).where(
            PickupCredential.session_token_hash == hash_secret(token)
        )
    )
    now = utc_now()
    if (
        credential is None
        or credential.consumed_at is None
        or as_utc(credential.session_expires_at) is None
        or as_utc(credential.session_expires_at) <= now
    ):
        raise HTTPException(401, {"code": "DELIVERY_PICKUP_SESSION_INVALID"})
    allocation = db.get(DeliveryAllocation, allocation_id)
    if (
        allocation is None
        or allocation.id != credential.allocation_id
        or allocation.status != "active"
        or as_utc(allocation.expires_at) <= now
    ):
        raise HTTPException(403, {"code": "DELIVERY_PICKUP_SCOPE_INVALID"})
    mailbox = db.get(InventoryMailbox, allocation.mailbox_id)
    if mailbox is None:
        raise HTTPException(404, {"code": "DELIVERY_MAILBOX_NOT_FOUND"})
    if mailbox.status != "allocated":
        raise HTTPException(403, {"code": "DELIVERY_PICKUP_SCOPE_INVALID"})
    messages = [
        {
            key: plain_text(message.get(key, ""))
            if key == "body_text"
            else message.get(key)
            for key in (
                "id",
                "sender",
                "subject",
                "received_at",
                "body_text",
                "verification_code",
            )
        }
        for message in mailbox.messages
    ]
    return {"address": mailbox.address, "messages": messages}

