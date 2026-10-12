"""Independent B1 persistence model; this package never imports mails_api."""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PublicMailbox(Base):
    __tablename__ = "public_mailboxes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    address_jwt_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), nullable=False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.id = str(kwargs.get("id", uuid4()))
        self.created_at = kwargs.get("created_at", utc_now())
        self.status = kwargs.get("status", "active")
