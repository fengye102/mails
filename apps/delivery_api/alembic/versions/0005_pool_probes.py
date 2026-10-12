"""E6：wait-message 异步探测表。

`pool_probes`：`mode=async` 时创建一行探测，`GET /probe/{id}` 轮询它。
探测不跑后台任务，状态由轮询触发推进（命中 found / 超时 timeout）。

Revision ID: 0005_pool_probes
Revises: 0004_pool_api_keys
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_pool_probes"
down_revision = "0004_pool_api_keys"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pool_probes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mailbox_id", sa.String(36), sa.ForeignKey("inventory_mailboxes.id"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pool_probes_mailbox_id", "pool_probes", ["mailbox_id"])
    op.create_index("ix_pool_probes_status", "pool_probes", ["status"])
    op.create_index("ix_pool_probes_created_at", "pool_probes", ["created_at"])


def downgrade():
    op.drop_index("ix_pool_probes_created_at", table_name="pool_probes")
    op.drop_index("ix_pool_probes_status", table_name="pool_probes")
    op.drop_index("ix_pool_probes_mailbox_id", table_name="pool_probes")
    op.drop_table("pool_probes")