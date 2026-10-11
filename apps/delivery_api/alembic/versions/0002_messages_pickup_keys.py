"""C2：邮件独立成表 + 取件密钥与会话

- 新建 `delivery_messages`，把 `inventory_mailboxes.messages` JSON 逐行回填后删除该列
  （不留双份真相）。回填复用 `delivery_api.messages.normalize_message`，
  保证历史邮件与今后推送进来的邮件走同一套规范化（HTML→纯文本、验证码、行动链接）。
- `pickup_credentials` 增加 `key_hash`（取件密钥摘要），`code_hash` 改为可空。
- 新建 `pickup_sessions`：同一凭证可并存多个会话（多标签页取件）。
  修订号控制在 32 字符内（alembic_version.version_num 是 varchar(32)）。
  旧的 `session_token_hash` / `session_expires_at` 两列随之删除——已签发的取件会话
  （最长 30 分钟）需要重新验证取件码/密钥，这是本次结构升级的预期代价。

Revision ID: 0002_messages_pickup_keys
Revises: 0001_delivery_initial
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from delivery_api.messages import normalize_message

revision = "0002_messages_pickup_keys"
down_revision = "0001_delivery_initial"
branch_labels = None
depends_on = None

MESSAGE_COLUMNS = (
    "id",
    "mailbox_id",
    "provider_message_id",
    "sender",
    "recipient",
    "subject",
    "body_text",
    "otp_code",
    "link",
    "received_at",
    "created_at",
)


def _messages_table() -> sa.Table:
    return sa.table("delivery_messages", *(sa.column(name) for name in MESSAGE_COLUMNS))


def upgrade():
    op.create_table(
        "delivery_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "mailbox_id",
            sa.String(36),
            sa.ForeignKey("inventory_mailboxes.id"),
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.String(256), nullable=False),
        sa.Column("sender", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("otp_code", sa.String(32)),
        sa.Column("link", sa.Text()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "mailbox_id",
            "provider_message_id",
            name="uq_delivery_messages_mailbox_provider",
        ),
    )
    op.create_index("ix_delivery_messages_mailbox_id", "delivery_messages", ["mailbox_id"])
    op.create_index(
        "ix_delivery_messages_mailbox_received",
        "delivery_messages",
        ["mailbox_id", "received_at"],
    )

    bind = op.get_bind()
    table = _messages_table()
    rows = bind.execute(sa.text("SELECT id, messages FROM inventory_mailboxes")).fetchall()
    for mailbox_id, payload in rows:
        items = payload if isinstance(payload, list) else json.loads(payload or "[]")
        for item in items or []:
            if not isinstance(item, dict):
                continue
            data = normalize_message(item)
            bind.execute(
                table.insert().values(
                    id=str(uuid4()),
                    mailbox_id=mailbox_id,
                    created_at=datetime.now(UTC),
                    **data,
                )
            )

    op.drop_column("inventory_mailboxes", "messages")

    op.add_column("pickup_credentials", sa.Column("key_hash", sa.String(64)))
    op.alter_column(
        "pickup_credentials",
        "code_hash",
        existing_type=sa.String(64),
        nullable=True,
    )
    op.create_index(
        "ix_pickup_credentials_key_hash",
        "pickup_credentials",
        ["key_hash"],
        unique=True,
    )
    op.create_table(
        "pickup_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "credential_id",
            sa.String(36),
            sa.ForeignKey("pickup_credentials.id"),
            nullable=False,
        ),
        sa.Column(
            "allocation_id",
            sa.String(36),
            sa.ForeignKey("delivery_allocations.id"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pickup_sessions_credential_id", "pickup_sessions", ["credential_id"])
    op.create_index("ix_pickup_sessions_allocation_id", "pickup_sessions", ["allocation_id"])
    op.create_index("ix_pickup_sessions_token_hash", "pickup_sessions", ["token_hash"], unique=True)

    op.drop_index("ix_pickup_credentials_session_token_hash", table_name="pickup_credentials")
    op.drop_column("pickup_credentials", "session_token_hash")
    op.drop_column("pickup_credentials", "session_expires_at")


def downgrade():
    op.add_column(
        "pickup_credentials",
        sa.Column("session_token_hash", sa.String(64)),
    )
    op.add_column(
        "pickup_credentials",
        sa.Column("session_expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_pickup_credentials_session_token_hash",
        "pickup_credentials",
        ["session_token_hash"],
    )
    for name in (
        "ix_pickup_sessions_token_hash",
        "ix_pickup_sessions_allocation_id",
        "ix_pickup_sessions_credential_id",
    ):
        op.drop_index(name, table_name="pickup_sessions")
    op.drop_table("pickup_sessions")
    op.drop_index("ix_pickup_credentials_key_hash", table_name="pickup_credentials")
    op.drop_column("pickup_credentials", "key_hash")
    op.alter_column(
        "pickup_credentials",
        "code_hash",
        existing_type=sa.String(64),
        nullable=False,
    )

    op.add_column(
        "inventory_mailboxes",
        sa.Column("messages", sa.JSON(), nullable=False, server_default="[]"),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT mailbox_id, provider_message_id, sender, recipient, subject,"
            " body_text, otp_code, link, received_at FROM delivery_messages"
        )
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(
            {
                "id": row[1],
                "sender": row[2],
                "recipient": row[3],
                "subject": row[4],
                "body_text": row[5],
                "verification_code": row[6],
                "link": row[7],
                "received_at": row[8].isoformat() if row[8] else None,
            }
        )
    for mailbox_id, items in grouped.items():
        bind.execute(
            sa.text("UPDATE inventory_mailboxes SET messages = :payload WHERE id = :id"),
            {"payload": json.dumps(items), "id": mailbox_id},
        )
    for name in ("ix_delivery_messages_mailbox_received", "ix_delivery_messages_mailbox_id"):
        op.drop_index(name, table_name="delivery_messages")
    op.drop_table("delivery_messages")
