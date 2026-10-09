import pytest
from mails_api.models import Base
from sqlalchemy import create_engine, inspect


def test_initial_schema_has_six_tables():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    assert set(inspect(engine).get_table_names()) == {
        "users",
        "sessions",
        "provider_connections",
        "mailboxes",
        "messages",
        "sync_runs",
    }


def test_schema_has_required_fields_and_constraints():
    from sqlalchemy import DateTime, LargeBinary, Uuid

    required = {
        "users": "id username password_hash role created_at",
        "sessions": "id user_id token_hash csrf_token created_at last_seen_at expires_at revoked_at",
        "provider_connections": "id kind name base_url encrypted_secret status last_error last_checked_at",
        "mailboxes": "id connection_id provider_mailbox_key address display_name sync_state last_synced_at",
        "messages": "id mailbox_id provider_message_key rfc_message_id sender recipient subject snippet received_at body_text verification_code created_at",
        "sync_runs": "id connection_id mailbox_id started_at finished_at status error_code error_summary new_message_count",
    }
    for name, fields in required.items():
        table = Base.metadata.tables[name]
        assert set(table.columns.keys()) == set(fields.split())
        assert isinstance(table.c.id.type, Uuid)
        assert table.c.id.type.as_uuid
        for column in table.columns:
            if column.name.endswith("_at"):
                assert isinstance(column.type, DateTime)
                assert column.type.timezone
    assert isinstance(
        Base.metadata.tables["provider_connections"].c.encrypted_secret.type,
        LargeBinary,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    schema = inspect(engine)
    assert {
        tuple(c["column_names"]) for c in schema.get_unique_constraints("mailboxes")
    } == {("connection_id", "provider_mailbox_key")}
    assert {
        tuple(c["column_names"]) for c in schema.get_unique_constraints("messages")
    } == {("mailbox_id", "provider_message_key")}
    for name, targets in {
        "sessions": {"users"},
        "mailboxes": {"provider_connections"},
        "messages": {"mailboxes"},
        "sync_runs": {"provider_connections", "mailboxes"},
    }.items():
        assert {fk["referred_table"] for fk in schema.get_foreign_keys(name)} == targets


def test_settings_require_environment_values(monkeypatch):
    import pytest
    from mails_api.settings import Settings
    from pydantic import ValidationError

    for key in ("DATABASE_URL", "ENCRYPTION_KEY", "PUBLIC_ORIGIN", "UPSTREAM_ORIGINS"):
        monkeypatch.delenv(f"MAILS_{key}", raising=False)
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("MAILS_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("MAILS_ENCRYPTION_KEY", "test-base64-key")
    monkeypatch.setenv("MAILS_PUBLIC_ORIGIN", "http://localhost:5173")
    monkeypatch.setenv("MAILS_UPSTREAM_ORIGINS", '["http://localhost:8001"]')
    settings = Settings()
    assert settings.database_url == "sqlite+pysqlite:///:memory:"
    assert settings.encryption_key == "test-base64-key"
    assert settings.public_origin == "http://localhost:5173"
    assert settings.upstream_origins == ["http://localhost:8001"]


@pytest.mark.asyncio
async def test_health_and_injected_state_without_implicit_schema(engine):
    import httpx
    from mails_api.app import create_app
    from mails_api.settings import Settings

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        encryption_key="key",
        public_origin="http://localhost:5173",
        upstream_origins=[],
    )
    upstream = httpx.AsyncClient()
    app = create_app(settings=settings, engine=engine, upstream_client=upstream)
    assert app.state.settings is settings
    assert app.state.engine is engine
    assert app.state.upstream_client is upstream
    async with (
        upstream,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    assert inspect(engine).get_table_names() == []


def test_get_db_uses_request_engine_and_rolls_back_uncommitted_work(engine):
    from datetime import datetime, timezone

    from fastapi import Request
    from mails_api.app import create_app
    from mails_api.db import get_db
    from mails_api.models import User
    from mails_api.settings import Settings
    from sqlalchemy import select
    from sqlalchemy.orm import Session as DatabaseSession

    Base.metadata.create_all(engine)
    app = create_app(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            encryption_key="key",
            public_origin="http://localhost",
            upstream_origins=[],
        ),
        engine,
    )
    request = Request({"type": "http", "app": app})
    dependency = get_db(request)
    db = next(dependency)
    assert isinstance(db, DatabaseSession)
    assert db.get_bind() is engine
    db.add(
        User(
            username="uncommitted",
            password_hash="hash",
            created_at=datetime.now(timezone.utc),
        )
    )
    db.flush()
    dependency.close()
    with DatabaseSession(engine) as check:
        assert check.scalar(select(User)) is None


def test_migration_upgrade_matches_models_and_downgrade(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    api_dir = Path(__file__).resolve().parents[1]
    config = Config(str(api_dir / "alembic.ini"))
    config.set_main_option("script_location", str(api_dir / "alembic"))
    url = f"sqlite+pysqlite:///{(tmp_path / 'migration.db').as_posix()}"
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    schema = inspect(engine)
    assert set(schema.get_table_names()) == set(Base.metadata.tables) | {
        "alembic_version"
    }
    for name, table in Base.metadata.tables.items():
        columns = {column["name"]: column for column in schema.get_columns(name)}
        assert set(columns) == set(table.columns.keys())
        for column in table.columns:
            assert columns[column.name]["nullable"] == column.nullable
        assert {
            tuple(c["column_names"]) for c in schema.get_unique_constraints(name)
        } == {
            tuple(c.columns.keys())
            for c in table.constraints
            if c.__class__.__name__ == "UniqueConstraint"
        }
    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()
