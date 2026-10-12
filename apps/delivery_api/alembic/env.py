import os

from alembic import context
from delivery_api.models import Base
from sqlalchemy import create_engine, pool

config = context.config
target_metadata = Base.metadata
url = config.get_main_option("sqlalchemy.url") or os.environ.get("DELIVERY_DATABASE_URL")


def run_migrations_offline():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


(run_migrations_offline if context.is_offline_mode() else run_migrations_online)()
