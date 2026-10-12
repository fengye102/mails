from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
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
    # 上次从上游拉信的时间。取件读用它做「同一邮箱 15 秒内只拉一次」的节流依据；
    # 也是「这批库存到底有没有在刷上游」的唯一凭证。
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 邮池状态（供注册机领取）。状态留在库存行上而不是只在领取表里，是为了让「领取」
    # 能用一条 UPDATE 原子完成（并发争抢只能有一个赢家）；领取表只做过程留痕。
    # 取值见 delivery_api.pool：available/claimed/used/cooldown/frozen/retired。
    pool_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="available", server_default="available", index=True
    )
    pool_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pool_cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class PoolApiKey(Base):
    """外部调用方（注册机）的池密钥。明文只在创建响应里出现一次，库里只存摘要。"""

    __tablename__ = "pool_api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # 归属项目：非空时请求里的 project_key 必须与它一致（项目隔离，见 pool.effective_project）。
    project: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 允许领取的域名白名单；空列表 = 不限。SQLite 没有数组类型，所以用 JSON 存。
    allowed_domains: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class PoolClaim(Base):
    """一次「领取 → 释放/回传」的留痕，对外就是契约里的 `account_id`。

    这里**不是**并发闸门：领取要在一个 UPDATE 里原子完成，所以邮箱的当前池状态落在
    `inventory_mailboxes.pool_status` 上；本表负责留痕（谁、何时、拿了哪个邮箱、结果如何）
    与「同 caller+项目只防 success」用的历史。

    主键用自增整数而不是 UUID：对外契约的 `account_id` 是整数（OEP 是账号自增 id），
    而邮箱 id 是 UUID 字符串——直接外露会破坏调用方的 `int(account_id)`。
    自增整数也不会像 SQLite rowid 那样在 VACUUM 后被重排，已发出的 account_id 始终有效。
    """

    __tablename__ = "pool_claims"
    __table_args__ = (
        # 租约回收（`status='claimed' AND lease_expires_at <= now`）按这两列扫。
        Index("ix_pool_claims_status_lease", "status", "lease_expires_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mailbox_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("inventory_mailboxes.id"), nullable=False, index=True
    )
    caller_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    task_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    project: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    claim_token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="claimed", index=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(String(32))
    result_detail: Mapped[str | None] = mapped_column(Text)
    # 可空的去重键：注册机重试同一任务时带上它，可以拿回同一条领取而不是再消耗一个邮箱。
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)


class PoolProbe(Base):
    """wait-message 异步模式的一次探测（`mode=async` 创建，`GET /probe/{id}` 轮询）。

    探测本身不跑后台任务：每次状态轮询（GET /probe）顺手触发一次上游刷新并检查
    自探测创建以来的新邮件，命中即置 `found`，超时即置 `timeout`。无后台线程，
    不会在生产里留孤儿进程。
    """

    __tablename__ = "pool_probes"
    __table_args__ = (
        Index("ix_pool_probes_status", "status"),
        Index("ix_pool_probes_created_at", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    mailbox_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("inventory_mailboxes.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 命中的消息形状 / 失败明细，序列化 JSON 存这里。
    result_json: Mapped[dict | None] = mapped_column(JSON)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
