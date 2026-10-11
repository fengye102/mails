from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


class InventoryMailbox(Base):
    __tablename__ = "inventory_mailboxes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    address: Mapped[str] = mapped_column(Text, nullable=False)
    source_label: Mapped[str] = mapped_column(String(64), nullable=False)
    access_token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="available", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class DeliveryMessage(Base):
    """取件邮件：按 (mailbox_id, provider_message_id) 幂等，重复推送只更新不新增。"""

    __tablename__ = "delivery_messages"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_id",
            "provider_message_id",
            name="uq_delivery_messages_mailbox_provider",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    mailbox_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("inventory_mailboxes.id"), nullable=False, index=True
    )
    provider_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    sender: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recipient: Mapped[str] = mapped_column(Text, nullable=False, default="")
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    otp_code: Mapped[str | None] = mapped_column(String(32))
    link: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class DeliveryAllocation(Base):
    __tablename__ = "delivery_allocations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    mailbox_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("inventory_mailboxes.id"), nullable=False, index=True
    )
    external_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", index=True)
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PickupCredential(Base):
    """取件凭证：`code`（一次性）或 `key`（可验证多次），明文不入库，只存摘要。"""

    __tablename__ = "pickup_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    allocation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("delivery_allocations.id"), nullable=False, index=True
    )
    code_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    key_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class PickupSession(Base):
    """取件会话：同一凭证可以并存多个会话（多标签页取件是正常用法）。"""

    __tablename__ = "pickup_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    credential_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pickup_credentials.id"), nullable=False, index=True
    )
    allocation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("delivery_allocations.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class PickupAttempt(Base):
    __tablename__ = "pickup_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class DeliveryAuditEvent(Base):
    __tablename__ = "delivery_audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    allocation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("delivery_allocations.id"), index=True
    )
    mailbox_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("inventory_mailboxes.id"), index=True
    )
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
