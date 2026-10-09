"""Explicit registration of mailboxes verified readable by the provider."""

import re
from typing import Annotated
from uuid import UUID

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, Request
from mails_api.db import get_db
from mails_api.models import Mailbox, ProviderConnection
from mails_api.providers.base import ProviderError
from mails_api.providers.cymail import mailbox_key as cymail_mailbox_key
from mails_api.routes.connections import (
    allowed_origin,
    checked_time,
    encryption_key,
    get_adapter_for,
    get_http_client,
)
from mails_api.security.crypto import decrypt_secret
from mails_api.security.csrf import require_csrf, require_member, require_owner
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_member)])


class MailboxInput(BaseModel):
    address: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=256)

    @field_validator("address")
    @classmethod
    def valid_address(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Invalid mailbox address")
        return value


def mailbox_payload(mailbox: Mailbox) -> dict:
    return {
        "id": str(mailbox.id),
        "connection_id": str(mailbox.connection_id),
        "provider_mailbox_key": mailbox.provider_mailbox_key,
        "address": mailbox.address,
        "display_name": mailbox.display_name,
        "sync_state": mailbox.sync_state,
        "last_synced_at": checked_time(mailbox.last_synced_at),
    }


@router.post(
    "/connections/{connection_id}/mailboxes",
    status_code=201,
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def create_mailbox(
    connection_id: UUID,
    body: MailboxInput,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> dict:
    connection = db.get(ProviderConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")
    origin = allowed_origin(connection.base_url, request)
    adapter = get_adapter_for(connection.kind, client)
    try:
        secret = decrypt_secret(encryption_key(request), connection.encrypted_secret)
    except (ValueError, InvalidTag, UnicodeError):
        raise HTTPException(
            status_code=503, detail="CONNECTION_SECRET_INVALID"
        ) from None
    provider_address = body.address
    if connection.kind == "cymail":
        account_id = (connection.config_json or {}).get("account_id")
        if not isinstance(account_id, str) or not account_id.strip():
            raise HTTPException(status_code=422, detail="CYMAIL_ACCOUNT_ID_REQUIRED")
        provider_address = cymail_mailbox_key(account_id, body.address)
    try:
        provider_key = await adapter.check_mailbox(
            origin, secret, provider_address, connection.config_json or {}
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=exc.code) from None
    mailbox = Mailbox(
        connection_id=connection_id,
        provider_mailbox_key=provider_key or provider_address,
        address=body.address,
        display_name=body.display_name,
        sync_state="idle",
    )
    db.add(mailbox)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="MAILBOX_ALREADY_REGISTERED"
        ) from None
    db.refresh(mailbox)
    return mailbox_payload(mailbox)


@router.get("/mailboxes")
def list_mailboxes(db: Annotated[DbSession, Depends(get_db)]) -> list[dict]:
    return [
        mailbox_payload(item)
        for item in db.scalars(select(Mailbox).order_by(Mailbox.id))
    ]
