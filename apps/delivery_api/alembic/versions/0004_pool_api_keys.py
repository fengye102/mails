"""E1：邮箱池（对外供注册机）的池密钥、领取留痕与邮箱池状态列

- `inventory_mailboxes` 加三列：
  - `pool_status`：池状态（available/claimed/used/cooldown/frozen/retired）。
    状态留在库存行上，是为了让「领取」能用**一条 UPDATE ... RETURNING** 原子完成——
    20 线程争同一个邮箱时，只有一个能把 `available` 改成 `claimed`。
  - `pool_lease_expires_at`：当前租约到期时间，回收任务的判据。
  - `pool_cooldown_until`：冷却截止时间（租约到期回收、或回传 verification_timeout 之后
    这段时间内不再发放）。
  既有行回填 `available`：平台库存就是池的资源（规格：池只是「带租约的分配」），
  能不能发由 `pool.eligible_conditions()` 统一判定（来源可读 + 库存可用 + 未过期），
  需求侧要排除某个邮箱时用运维接口 `PATCH /delivery-api/v1/pool/mailboxes/{id}`。
- `pool_api_keys`：对外 Key 只存 sha256 摘要，明文只在创建响应里出现一次。
- `pool_claims`：一次「领取 → 释放/回传」一行；主键是自增整数（对外即 `account_id`，
  契约要求整数，且不像 rowid 会被 VACUUM 重排）。

Revision ID: 0004_pool_api_keys
Revises: 0003_mailbox_refresh_state
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_pool_api_keys"
down_revision = "0003_mailbox_refresh_state"
branch_labels = None
depends_on = None


def upgrade():
    for name, column in (
        ("pool_status", sa.Column("pool_status", sa.String(24), nullable=False, server_default="available")),
        ("pool_lease_expires_at", sa.Column("pool_lease_expires_at", sa.DateTime(timezone=True), nullable=True)),
        ("pool_cooldown_until", sa.Column("pool_cooldown_until", sa.DateTime(timezone=True), nullable=True)),
    ):
        op.add_column("inventory_mailboxes", column)
    op.create_index("ix_inventory_mailboxes_pool_status", "inventory_mailboxes", ["pool_status"])

    op.create_table(
        "pool_api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, server_default=""),
        sa.Column("project", sa.String(128), nullable=False, server_default=""),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("allowed_domains", sa.JSON(), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pool_api_keys_key_hash", "pool_api_keys", ["key_hash"], unique=True)

    op.create_table(
        "pool_claims",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mailbox_id", sa.String(36), sa.ForeignKey("inventory_mailboxes.id"), nullable=False),
        sa.Column("caller_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("task_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("project", sa.String(128), nullable=False, server_default=""),
        sa.Column("claim_token", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="claimed"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.String(32), nullable=True),
        sa.Column("result_detail", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
    )
    op.create_index("ix_pool_claims_mailbox_id", "pool_claims", ["mailbox_id"])
    op.create_index("ix_pool_claims_status", "pool_claims", ["status"])
    op.create_index("ix_pool_claims_claim_token", "pool_claims", ["claim_token"], unique=True)
    op.create_index("ix_pool_claims_idempotency_key", "pool_claims", ["idempotency_key"], unique=True)
    # 租约回收与「在这台邮箱上抢过的赢家」都按 (status, lease_expires_at) 扫。
    op.create_index("ix_pool_claims_status_lease", "pool_claims", ["status", "lease_expires_at"])


def downgrade():
    op.drop_table("pool_claims")
    op.drop_table("pool_api_keys")
    op.drop_index("ix_inventory_mailboxes_pool_status", table_name="inventory_mailboxes")
    for name in ("pool_cooldown_until", "pool_lease_expires_at", "pool_status"):
        op.drop_column("inventory_mailboxes", name)
