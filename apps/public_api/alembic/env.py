"""B1 Alembic environment."""
import os

from public_mail_api.models import Base
from sqlalchemy import create_engine, pool

from alembic import context

config = context.config
target_metadata = Base.metadata


def database_url():
    return config.get_main_option("sqlalchemy.url") or os.environ.get("PUBLIC_MAIL_DATABASE_URL")


def run_migrations_offline():
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


(run_migrations_offline if context.is_offline_mode() else run_migrations_online)()
