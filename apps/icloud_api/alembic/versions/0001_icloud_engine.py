import sqlalchemy as sa

from alembic import op

revision = "0001_icloud_engine"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "icloud_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("real_email", sa.Text(), nullable=False),
        sa.Column("icloud_email", sa.Text(), nullable=False),
        sa.Column("cookies_enc", sa.LargeBinary(), nullable=False),
        sa.Column("host", sa.String(128), nullable=False),
        sa.Column("proxy", sa.String(256), nullable=False),
        sa.Column("app_password_enc", sa.LargeBinary(), nullable=False),
        sa.Column("apple_session_enc", sa.LargeBinary(), nullable=False),
        sa.Column("forward_to_emails", sa.JSON()),
        sa.Column("selected_forward_to", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("alias_total", sa.Integer(), nullable=False),
        sa.Column("alias_active", sa.Integer(), nullable=False),
        sa.Column("last_validated", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_icloud_accounts_status", "icloud_accounts", ["status"])

    op.create_table(
        "icloud_aliases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("icloud_accounts.id"), nullable=False),
        sa.Column("anonymous_id", sa.String(128), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "address", name="uq_icloud_aliases_account_address"),
    )
    op.create_index("ix_icloud_aliases_account_id", "icloud_aliases", ["account_id"])
    op.create_index("ix_icloud_aliases_address", "icloud_aliases", ["address"])
    op.create_index("ix_icloud_aliases_state", "icloud_aliases", ["state"])

    op.create_table(
        "icloud_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("icloud_accounts.id"), nullable=False),
        sa.Column("alias_address", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.String(256), nullable=False),
        sa.Column("sender", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("otp_code", sa.String(32)),
        sa.Column("link", sa.Text()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "provider_message_id", name="uq_icloud_messages_account_provider"),
    )
    op.create_index("ix_icloud_messages_account_id", "icloud_messages", ["account_id"])
    op.create_index("ix_icloud_messages_alias_address", "icloud_messages", ["alias_address"])

    op.create_table(
        "icloud_scheduler_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("icloud_accounts.id"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("events", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_icloud_scheduler_jobs_account_id", "icloud_scheduler_jobs", ["account_id"])

    op.create_table(
        "icloud_browser_auth",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("icloud_accounts.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_icloud_browser_auth_account_id", "icloud_browser_auth", ["account_id"])
    op.create_index("ix_icloud_browser_auth_status", "icloud_browser_auth", ["status"])
    op.create_index("ix_icloud_browser_auth_token_hash", "icloud_browser_auth", ["token_hash"], unique=True)


def downgrade():
    op.drop_index("ix_icloud_browser_auth_token_hash", table_name="icloud_browser_auth")
    op.drop_index("ix_icloud_browser_auth_status", table_name="icloud_browser_auth")
    op.drop_index("ix_icloud_browser_auth_account_id", table_name="icloud_browser_auth")
    op.drop_table("icloud_browser_auth")
    op.drop_index("ix_icloud_scheduler_jobs_account_id", table_name="icloud_scheduler_jobs")
    op.drop_table("icloud_scheduler_jobs")
    op.drop_index("ix_icloud_messages_alias_address", table_name="icloud_messages")
    op.drop_index("ix_icloud_messages_account_id", table_name="icloud_messages")
    op.drop_table("icloud_messages")
    op.drop_index("ix_icloud_aliases_state", table_name="icloud_aliases")
    op.drop_index("ix_icloud_aliases_address", table_name="icloud_aliases")
    op.drop_index("ix_icloud_aliases_account_id", table_name="icloud_aliases")
    op.drop_table("icloud_aliases")
    op.drop_index("ix_icloud_accounts_status", table_name="icloud_accounts")
    op.drop_table("icloud_accounts")
