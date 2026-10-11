"""iCloud 引擎数据模型（阶段 D 规格：迁移 0001）。

凭据列一律密文：`cookies_enc`、`app_password_enc`、`apple_session_enc` 由
`icloud_api.security.crypto` 加密，明文不入库、不进日志、不进响应体。
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
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


class ICloudAccount(Base):
    """一个 iCloud 账号：Cookie 会话、App Password、Apple Account 会话各自加密存放。"""

    __tablename__ = "icloud_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    real_email: Mapped[str] = mapped_column(Text, nullable=False, default="")
    icloud_email: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cookies_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")
    host: Mapped[str] = mapped_column(String(128), nullable=False, default="icloud.com")
    proxy: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    app_password_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")
    apple_session_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")
    forward_to_emails: Mapped[list | None] = mapped_column(JSON)
    selected_forward_to: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", index=True)
    alias_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alias_active: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_validated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ICloudAlias(Base):
    """隐藏邮箱别名库存：上游同步 upsert，幽灵行在同步时清理。"""

    __tablename__ = "icloud_aliases"
    __table_args__ = (
        UniqueConstraint("account_id", "address", name="uq_icloud_aliases_account_address"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("icloud_accounts.id"), nullable=False, index=True
    )
    anonymous_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    address: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ICloudMessage(Base):
    """引擎侧邮件：按 (account_id, provider_message_id) 幂等。"""

    __tablename__ = "icloud_messages"
    __table_args__ = (
        UniqueConstraint("account_id", "provider_message_id", name="uq_icloud_messages_account_provider"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("icloud_accounts.id"), nullable=False, index=True
    )
    alias_address: Mapped[str] = mapped_column(Text, nullable=False, default="", index=True)
    provider_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    sender: Mapped[str] = mapped_column(Text, nullable=False, default="")
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    otp_code: Mapped[str | None] = mapped_column(String(32))
    link: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ICloudSchedulerJob(Base):
    """服务端定时创建任务：`enabled` 默认 false，必须由界面显式打开。"""

    __tablename__ = "icloud_scheduler_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("icloud_accounts.id"), nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    events: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ICloudBrowserAuthSession(Base):
    """浏览器授权会话：一次性授权码只存摘要，TTL 到期即失效。"""

    __tablename__ = "icloud_browser_auth"
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("icloud_accounts.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="icloud")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
