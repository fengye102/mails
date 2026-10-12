"""C 站：库存行增加上游刷新时间 `last_refreshed_at`

取件读时要按邮箱节流刷新上游（同一邮箱 15 秒内只拉一次）。节流依据必须
跨进程、跨重启有效，所以落在库存行上，而不是进程内字典——多 worker 部署
或进程重启都会让内存版本失效（退化成每个取件请求都去打上游 IMAP）。

可空、无默认值：历史行 `NULL` 表示「还没刷过」，首次取件读就会刷一次。

Revision ID: 0003_mailbox_refresh_state
Revises: 0002_messages_pickup_keys
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_mailbox_refresh_state"
down_revision = "0002_messages_pickup_keys"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "inventory_mailboxes",
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("inventory_mailboxes", "last_refreshed_at")
