"""Add non-secret provider configuration."""

from alembic import op
import sqlalchemy as sa

revision = "0002_provider_config"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("provider_connections", sa.Column("config_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))

def downgrade() -> None:
    op.drop_column("provider_connections", "config_json")

