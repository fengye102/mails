import sqlalchemy as sa
from alembic import op

revision = "0001_delivery_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("inventory_mailboxes", sa.Column("id", sa.String(36), primary_key=True), sa.Column("address", sa.Text(), nullable=False), sa.Column("source_label", sa.String(64), nullable=False), sa.Column("access_token_ciphertext", sa.LargeBinary(), nullable=False), sa.Column("messages", sa.JSON(), nullable=False), sa.Column("status", sa.String(24), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_inventory_mailboxes_status", "inventory_mailboxes", ["status"])
    op.create_table("delivery_allocations", sa.Column("id", sa.String(36), primary_key=True), sa.Column("mailbox_id", sa.String(36), sa.ForeignKey("inventory_mailboxes.id"), nullable=False), sa.Column("external_ref", sa.String(256), nullable=False), sa.Column("status", sa.String(24), nullable=False), sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_delivery_allocations_mailbox_id", "delivery_allocations", ["mailbox_id"])
    op.create_index("ix_delivery_allocations_status", "delivery_allocations", ["status"])
    op.create_table("pickup_credentials", sa.Column("id", sa.String(36), primary_key=True), sa.Column("allocation_id", sa.String(36), sa.ForeignKey("delivery_allocations.id"), nullable=False), sa.Column("code_hash", sa.String(64), nullable=False, unique=True), sa.Column("session_token_hash", sa.String(64)), sa.Column("session_expires_at", sa.DateTime(timezone=True)), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("consumed_at", sa.DateTime(timezone=True)), sa.Column("failed_attempts", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_pickup_credentials_allocation_id", "pickup_credentials", ["allocation_id"])
    op.create_index("ix_pickup_credentials_code_hash", "pickup_credentials", ["code_hash"], unique=True)
    op.create_index("ix_pickup_credentials_session_token_hash", "pickup_credentials", ["session_token_hash"])
    op.create_table("delivery_audit_events", sa.Column("id", sa.String(36), primary_key=True), sa.Column("event_type", sa.String(64), nullable=False), sa.Column("allocation_id", sa.String(36), sa.ForeignKey("delivery_allocations.id")), sa.Column("mailbox_id", sa.String(36), sa.ForeignKey("inventory_mailboxes.id")), sa.Column("details", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_delivery_audit_events_event_type", "delivery_audit_events", ["event_type"])
    op.create_index("ix_delivery_audit_events_allocation_id", "delivery_audit_events", ["allocation_id"])
    op.create_index("ix_delivery_audit_events_mailbox_id", "delivery_audit_events", ["mailbox_id"])


def downgrade():
    for name, table in [("ix_delivery_audit_events_mailbox_id", "delivery_audit_events"), ("ix_delivery_audit_events_allocation_id", "delivery_audit_events"), ("ix_delivery_audit_events_event_type", "delivery_audit_events")]: op.drop_index(name, table_name=table)
    op.drop_table("delivery_audit_events")
    for name, table in [("ix_pickup_credentials_session_token_hash", "pickup_credentials"), ("ix_pickup_credentials_code_hash", "pickup_credentials"), ("ix_pickup_credentials_allocation_id", "pickup_credentials")]: op.drop_index(name, table_name=table)
    op.drop_table("pickup_credentials")
    for name, table in [("ix_delivery_allocations_status", "delivery_allocations"), ("ix_delivery_allocations_mailbox_id", "delivery_allocations")]: op.drop_index(name, table_name=table)
    op.drop_table("delivery_allocations")
    op.drop_index("ix_inventory_mailboxes_status", table_name="inventory_mailboxes")
    op.drop_table("inventory_mailboxes")

