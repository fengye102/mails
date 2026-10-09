"""Conservative, read-only message identity signals."""

import re
from collections import Counter
from collections.abc import Sequence

from mails_api.models import Message
from sqlalchemy import select
from sqlalchemy.orm import Session

_RFC_MESSAGE_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")


def _valid_rfc_message_id(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _RFC_MESSAGE_ID.fullmatch(value) else None


def identity_statuses(db: Session, messages: Sequence[Message]) -> dict:
    """Return audit status per message without merging or changing rows."""
    if not messages:
        return {}

    mailbox_ids = {message.mailbox_id for message in messages}
    rows = db.execute(
        select(Message.mailbox_id, Message.rfc_message_id).where(
            Message.mailbox_id.in_(mailbox_ids)
        )
    ).all()
    counts: Counter[tuple[object, str]] = Counter()
    for mailbox_id, rfc_message_id in rows:
        normalized = _valid_rfc_message_id(rfc_message_id)
        if normalized is not None:
            counts[(mailbox_id, normalized)] += 1

    return {
        message.id: (
            "same_rfc_id"
            if (normalized := _valid_rfc_message_id(message.rfc_message_id))
            and counts[(message.mailbox_id, normalized)] > 1
            else "unverified"
        )
        for message in messages
    }
