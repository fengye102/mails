"""Disposable PostgreSQL + HTTP transport integration; never substitutes SQLite."""

import base64
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from mails_api.app import create_app
from mails_api.cli import main
from mails_api.models import Base
from mails_api.settings import Settings
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url


def _assert_postgresql_schema(engine):
    """Acceptance checks against the migrated PostgreSQL schema, not create_all."""
    assert engine.dialect.name == "postgresql"
    schema = inspect(engine)
    assert set(schema.get_table_names()) == {
        "users",
        "sessions",
        "provider_connections",
        "mailboxes",
        "messages",
        "sync_runs",
        "alembic_version",
    }
    foreign_keys = {
        "users": set(),
        "sessions": {(("user_id",), "users", ("id",))},
        "provider_connections": set(),
        "mailboxes": {(("connection_id",), "provider_connections", ("id",))},
        "messages": {(("mailbox_id",), "mailboxes", ("id",))},
        "sync_runs": {
            (("connection_id",), "provider_connections", ("id",)),
            (("mailbox_id",), "mailboxes", ("id",)),
        },
    }
    unique_keys = {
        "users": {("username",)},
        "sessions": set(),
        "provider_connections": set(),
        "mailboxes": {("connection_id", "provider_mailbox_key")},
        "messages": {("mailbox_id", "provider_message_key")},
        "sync_runs": set(),
    }
    for name, table in Base.metadata.tables.items():
        assert schema.get_pk_constraint(name)["constrained_columns"] == ["id"], name
        assert {
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
            )
            for fk in schema.get_foreign_keys(name)
        } == foreign_keys[name], name
        assert {
            tuple(item["column_names"]) for item in schema.get_unique_constraints(name)
        } == unique_keys[name], name
        columns = {column["name"]: column for column in schema.get_columns(name)}
        assert set(columns) == set(table.columns.keys()), name
        for column in table.columns:
            actual = columns[column.name]
            assert actual["nullable"] == column.nullable, (name, column.name)
            actual_type = actual["type"].compile(dialect=engine.dialect)
            expected_type = column.type.compile(dialect=engine.dialect)
            assert actual_type == expected_type, (
                name,
                column.name,
                actual_type,
                expected_type,
            )
            if column.name == "id" or column.foreign_keys:
                assert actual_type == "UUID", (name, column.name)
            if column.name.endswith("_at"):
                assert actual_type == "TIMESTAMP WITH TIME ZONE", (name, column.name)
        if name == "provider_connections":
            assert (
                columns["encrypted_secret"]["type"].compile(dialect=engine.dialect)
                == "BYTEA"
            )


@pytest.fixture
def api_client(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("SKIPPED: TEST_DATABASE_URL missing (PostgreSQL prerequisite)")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (
        parsed.database or ""
    ).startswith("mails_test"):
        pytest.fail(
            "Refusing setup: PostgreSQL database name must begin with mails_test"
        )
    # Fail closed: libpq/driver query options can override database/host/service.
    # This disposable fixture accepts no URL query options, including benign ones.
    if parsed.query:
        pytest.fail("Refusing setup: TEST_DATABASE_URL query parameters are forbidden")
    engine = create_engine(url)
    root = Path(__file__).resolve().parents[2]
    migration = Config(str(root / "apps/api/alembic.ini"))
    migration.set_main_option("script_location", str(root / "apps/api/alembic"))
    migration.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    # Require a fresh database; never drop an existing application's tables.
    try:
        with engine.connect() as connection:
            actual_database = connection.exec_driver_sql(
                "SELECT current_database()"
            ).scalar_one()
        if (
            not actual_database.startswith("mails_test")
            or actual_database != parsed.database
        ):
            pytest.fail(
                "Refusing setup: connected database is not the requested mails_test database"
            )
        if inspect(engine).get_table_names():
            pytest.fail("Refusing setup: mails_test database must be empty")
        command.upgrade(migration, "head")
        try:
            _assert_postgresql_schema(engine)
            monkeypatch.setenv("MAILS_DATABASE_URL", url)
            monkeypatch.setenv(
                "MAILS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode()
            )
            monkeypatch.setenv("MAILS_PUBLIC_ORIGIN", "https://hub.example")
            monkeypatch.setenv("MAILS_UPSTREAM_ORIGINS", '["https://mail.example"]')
            monkeypatch.setattr("getpass.getpass", lambda _: "integration-password")
            assert main(["create-owner", "owner"]) == 0
            state = {"details": 0}
            item = {
                "id": "one",
                "method": "Graph API",
                "timestamp": datetime.now(timezone.utc).timestamp(),
                "from_address": "sender@example.com",
                "subject": "Verification code",
                "content_preview": "Your verification code",
            }

            def upstream(request):
                assert request.headers["X-API-Key"] == "integration-test-key"
                path = request.url.path
                if path == "/api/external/health":
                    data = {"status": "ok", "database": "ok"}
                elif path == "/api/external/capabilities":
                    data = {"features": ["message_list", "message_detail"]}
                elif path == "/api/external/account-status":
                    data = {
                        "exists": True,
                        "can_read": request.url.params["email"]
                        in ("a@example.com", "b@example.com"),
                    }
                elif path == "/api/external/messages":
                    data = {
                        "emails": [item]
                        if request.url.params["email"] == "a@example.com"
                        else [],
                        "has_more": False,
                    }
                elif path == "/api/external/messages/one":
                    state["details"] += 1
                    data = {
                        **item,
                        "content": "Your verification code: 123456",
                        "to_address": "a@example.com",
                        "raw_content": "",
                    }
                else:
                    raise AssertionError(f"Unexpected upstream path: {path}")
                return httpx.Response(
                    200, json={"success": True, "code": "OK", "data": data}
                )

            upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
            with TestClient(
                create_app(Settings(), engine, upstream_client),
                base_url="https://hub.example",
            ) as client:
                try:
                    yield client, state, engine
                finally:
                    client.portal.call(upstream_client.aclose)
        finally:
            command.downgrade(migration, "base")
            # Downgrade leaves Alembic's empty bookkeeping table.
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP TABLE alembic_version")
    finally:
        engine.dispose()


@pytest.fixture
def session_headers(api_client):
    client, _, _ = api_client
    response = client.post(
        "/api/v1/session",
        headers={"Origin": "https://hub.example"},
        json={"username": "owner", "password": "integration-password"},
    )
    assert response.status_code == 200, response.text
    return {
        "Origin": "https://hub.example",
        "X-CSRF-Token": response.json()["csrf_token"],
    }


@pytest.fixture
def integration_namespace():
    return globals()
