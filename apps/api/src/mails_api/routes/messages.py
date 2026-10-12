"""Private local message search and JSON-only plain text detail."""

from datetime import UTC
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import load_only

from mails_api.db import get_db
from mails_api.models import Mailbox, Message, ProviderConnection
from mails_api.security.csrf import require_member
from mails_api.services.extract import extract_link
from mails_api.services.identity import identity_statuses

router = APIRouter(
    prefix="/api/v1/messages", dependencies=[Depends(require_member)]
)


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def message_payload(
    message: Message, source_kind: str, identity_status: str = "unverified"
) -> dict:
    # SQLite omits timezone information; persisted dates are always UTC.
    received_at = message.received_at
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    return {
        "id": str(message.id),
        "mailbox_id": str(message.mailbox_id),
        "source_kind": source_kind,
        "identity_status": identity_status,
        "sender": message.sender,
        "recipient": message.recipient,
        "subject": message.subject,
        "snippet": message.snippet,
        "received_at": received_at.astimezone(UTC).isoformat(),
        "verification_code": message.verification_code,
    }


@router.get("")
def list_messages(
    db: Annotated[DbSession, Depends(get_db)],
    mailbox_id: UUID | None = None,
    source_kind: str | None = None,
    connection_id: UUID | None = None,
    query: str | None = None,
    sender: str | None = None,
    subject: str | None = None,
    from_time: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    to_time: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> dict:
    statement = select(Message, ProviderConnection.kind).join(
        Mailbox, Message.mailbox_id == Mailbox.id
    ).join(ProviderConnection, Mailbox.connection_id == ProviderConnection.id)
    if mailbox_id is not None:
        statement = statement.where(Message.mailbox_id == mailbox_id)
    if source_kind is not None:
        statement = statement.where(ProviderConnection.kind == source_kind)
    if connection_id is not None:
        statement = statement.where(Mailbox.connection_id == connection_id)
    for column, value in [(Message.sender, sender), (Message.subject, subject)]:
        if value is not None:
            statement = statement.where(
                column.ilike(f"%{escape_like(value)}%", escape="\\")
            )
    if query is not None:
        pattern = f"%{escape_like(query)}%"
        statement = statement.where(
            or_(
                Message.sender.ilike(pattern, escape="\\"),
                Message.recipient.ilike(pattern, escape="\\"),
                Message.subject.ilike(pattern, escape="\\"),
                Message.snippet.ilike(pattern, escape="\\"),
                Message.body_text.ilike(pattern, escape="\\"),
            )
        )
    if from_time is not None:
        statement = statement.where(
            Message.received_at >= from_time.astimezone(UTC)
        )
    if to_time is not None:
        statement = statement.where(
            Message.received_at < to_time.astimezone(UTC)
        )
    total = db.scalar(
        statement.with_only_columns(func.count(), maintain_column_froms=True)
    )
    statement = (
        statement.options(
            load_only(
                Message.id,
                Message.mailbox_id,
                Message.rfc_message_id,
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
    rows = list(db.execute(statement).all())
    messages = [message for message, _source_kind in rows]
    statuses = identity_statuses(db, messages)
    return {
        "total": total,
        "items": [
            message_payload(message, kind, statuses[message.id])
            for (message, kind) in rows
        ],
    }


@router.get("/{message_id}")
def get_message(message_id: UUID, db: Annotated[DbSession, Depends(get_db)]) -> dict:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="MESSAGE_NOT_FOUND")
    source_kind = db.scalar(
        select(ProviderConnection.kind)
        .join(Mailbox, Mailbox.connection_id == ProviderConnection.id)
        .where(Mailbox.id == message.mailbox_id)
    )
    statuses = identity_statuses(db, [message])
    return {
        **message_payload(message, source_kind or "unknown", statuses[message.id]),
        "body_text": message.body_text,
        "link": extract_link(message.subject, message.body_text),
    }
