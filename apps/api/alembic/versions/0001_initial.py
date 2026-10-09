"""Initial unified core schema.

Revision ID: 0001_initial
Revises: None
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("encrypted_secret", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "mailboxes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "connection_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("provider_connections.id"),
            nullable=False,
        ),
        sa.Column("provider_mailbox_key", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("sync_state", sa.String(32), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "connection_id", "provider_mailbox_key", name="uq_mailboxes_provider_key"
        ),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "mailbox_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("mailboxes.id"),
            nullable=False,
        ),
        sa.Column("provider_message_key", sa.Text(), nullable=False),
        sa.Column("rfc_message_id", sa.Text()),
        sa.Column("sender", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("verification_code", sa.String(8)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "mailbox_id", "provider_message_key", name="uq_messages_provider_key"
        ),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "connection_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("provider_connections.id"),
            nullable=False,
        ),
        sa.Column(
            "mailbox_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("mailboxes.id"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_summary", sa.Text()),
        sa.Column("new_message_count", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    for name in (
        "sync_runs",
        "messages",
        "mailboxes",
        "provider_connections",
        "sessions",
        "users",
    ):
        op.drop_table(name)
