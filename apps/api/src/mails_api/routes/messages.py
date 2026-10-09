"""Private local message search and JSON-only plain text detail."""

from datetime import timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from mails_api.db import get_db
from mails_api.models import Message
from mails_api.security.sessions import get_current_session
from pydantic import AwareDatetime
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import load_only

router = APIRouter(
    prefix="/api/v1/messages", dependencies=[Depends(get_current_session)]
)


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def message_payload(message: Message) -> dict:
    # SQLite omits timezone information; persisted dates are always UTC.
    received_at = message.received_at
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return {
        "id": str(message.id),
        "mailbox_id": str(message.mailbox_id),
        "sender": message.sender,
        "recipient": message.recipient,
        "subject": message.subject,
        "snippet": message.snippet,
        "received_at": received_at.astimezone(timezone.utc).isoformat(),
        "verification_code": message.verification_code,
    }


@router.get("")
def list_messages(
    db: Annotated[DbSession, Depends(get_db)],
    mailbox_id: UUID | None = None,
    sender: str | None = None,
    subject: str | None = None,
    from_time: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    to_time: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> dict:
    query = select(Message)
    if mailbox_id is not None:
        query = query.where(Message.mailbox_id == mailbox_id)
    for column, value in [(Message.sender, sender), (Message.subject, subject)]:
        if value is not None:
            query = query.where(column.ilike(f"%{escape_like(value)}%", escape="\\"))
    if from_time is not None:
        query = query.where(Message.received_at >= from_time.astimezone(timezone.utc))
    if to_time is not None:
        query = query.where(Message.received_at < to_time.astimezone(timezone.utc))
    total = db.scalar(query.with_only_columns(func.count(), maintain_column_froms=True))
    query = (
        query.options(
            load_only(
                Message.id,
                Message.mailbox_id,
                Message.sender,
                Message.recipient,
                Message.subject,
                Message.snippet,
                Message.received_at,
                Message.verification_code,
            )
        )
        .order_by(Message.received_at.desc(), Message.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return {
        "total": total,
        "items": [message_payload(item) for item in db.scalars(query)],
    }


@router.get("/{message_id}")
def get_message(message_id: UUID, db: Annotated[DbSession, Depends(get_db)]) -> dict:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="MESSAGE_NOT_FOUND")
    return {**message_payload(message), "body_text": message.body_text}
