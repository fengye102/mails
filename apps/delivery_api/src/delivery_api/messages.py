"""邮件入库：幂等 upsert 与入库规范化。

与 CYMail 的 `store.SaveMessage`（按 account/provider_message_id/mailbox 唯一、
`ON CONFLICT DO UPDATE`）保持同一语义：同一封上游邮件重复推送只更新，不新增行，
因此「定时推送」与「取件前补推」并发调用也是安全的。

入库即规范化：
- HTML 正文转纯文本（保留换行与 http(s) 链接目标）；
- 服务端提取验证码与行动链接，调用方给的字段优先；
- 缺 `provider_message_id` 时用内容指纹兜底，保证幂等键始终存在。
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from delivery_api.extract import extract_code, extract_link
from delivery_api.htmltext import html_to_text, looks_like_html
from delivery_api.models import DeliveryMessage, utc_now

# 正文上限：够放下整页转文本后的内容，又能挡住超大垃圾邮件。
MAX_BODY_CHARS = 20000
BODY_KEYS = ("body_text", "body", "text", "content", "preview")
SENDER_KEYS = ("sender", "from_address", "from")
RECIPIENT_KEYS = ("recipient", "to_address", "to")
TIME_KEYS = ("received_at", "date", "created_at")
ID_KEYS = ("provider_message_id", "id", "message_id", "uid")


@dataclass(frozen=True)
class IngestOutcome:
    inserted: int
    updated: int
    total: int

    def as_dict(self) -> dict[str, int]:
        return {"inserted": self.inserted, "updated": self.updated, "total": self.total}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return _text(value)
    return ""


def parse_received_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            # Python 3.11+ 的 fromisoformat 直接接受结尾的 Z（容器是 3.12）。
            moment = datetime.fromisoformat(value.strip())
        except ValueError:
            moment = utc_now()
    else:
        moment = utc_now()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def content_fingerprint(sender: str, subject: str, received_at: datetime) -> str:
    raw = f"{sender}|{subject}|{received_at.isoformat()}".encode()
    return "fp_" + hashlib.sha256(raw).hexdigest()[:48]


def normalize_message(item: dict[str, Any]) -> dict[str, Any]:
    sender = _first(item, SENDER_KEYS)
    recipient = _first(item, RECIPIENT_KEYS)
    subject = _text(item.get("subject"))
    received_at = parse_received_at(_first(item, TIME_KEYS) or None)

    body = _first(item, BODY_KEYS)
    if looks_like_html(body):
        body = html_to_text(body)

    # 提取必须在截断之前：行动链接可能落在很靠后的位置。
    otp_code = _text(item.get("otp_code") or item.get("verification_code"))
    link = _text(item.get("link"))
    if not otp_code:
        otp_code = extract_code(subject, body) or ""
    if not link:
        link = extract_link(subject, body) or ""

    provider_message_id = _first(item, ID_KEYS).strip()
    if not provider_message_id:
        provider_message_id = content_fingerprint(sender, subject, received_at)

    return {
        "provider_message_id": provider_message_id[:256],
        "sender": sender,
        "recipient": recipient,
        "subject": subject,
        "body_text": body[:MAX_BODY_CHARS],
        "otp_code": otp_code[:32] or None,
        "link": link or None,
        "received_at": received_at,
    }


def ingest_messages(db: Session, mailbox_id: str, items: list[dict[str, Any]]) -> IngestOutcome:
    """幂等写入一批邮件并提交；返回本次插入/更新与邮箱内总数。"""
    inserted = 0
    updated = 0
    for raw in items:
        data = normalize_message(raw)
        existing = db.scalar(
            select(DeliveryMessage)
            .where(
                DeliveryMessage.mailbox_id == mailbox_id,
                DeliveryMessage.provider_message_id == data["provider_message_id"],
            )
            .with_for_update()
        )
        if existing is None:
            db.add(DeliveryMessage(mailbox_id=mailbox_id, **data))
            inserted += 1
        else:
            for key, value in data.items():
                setattr(existing, key, value)
            updated += 1
    db.commit()
    return IngestOutcome(inserted, updated, count_messages(db, mailbox_id))


def count_messages(db: Session, mailbox_id: str) -> int:
    total = db.scalar(
        select(func.count())
        .select_from(DeliveryMessage)
        .where(DeliveryMessage.mailbox_id == mailbox_id)
    )
    return int(total or 0)


def last_ingested_at(db: Session, mailbox_id: str) -> datetime | None:
    return db.scalar(
        select(func.max(DeliveryMessage.created_at)).where(
            DeliveryMessage.mailbox_id == mailbox_id
        )
    )


def list_messages(db: Session, mailbox_id: str, limit: int) -> list[DeliveryMessage]:
    return list(
        db.scalars(
            select(DeliveryMessage)
            .where(DeliveryMessage.mailbox_id == mailbox_id)
            .order_by(DeliveryMessage.received_at.desc(), DeliveryMessage.id.desc())
            .limit(limit)
        ).all()
    )
