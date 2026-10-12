"""Initial isolated B1 public mailbox schema."""
import sqlalchemy as sa

from alembic import op

revision = "0001_public_mailboxes"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Keep the migration aligned with the SQLAlchemy model and SQLite contract
    # tests. The upstream address is not a durable identity: providers may
    # recycle an address after expiry, so uniqueness belongs to the upstream
    # allocation contract rather than this local table.
    op.create_table(
        "public_mailboxes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("address_jwt_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(24), nullable=False),
    )
    op.create_index("ix_public_mailboxes_token_hash", "public_mailboxes", ["token_hash"])


def downgrade():
    op.drop_index("ix_public_mailboxes_token_hash", table_name="public_mailboxes")
    op.drop_table("public_mailboxes")
