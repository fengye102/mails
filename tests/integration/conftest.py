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
from mails_api.settings import Settings
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url


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
    engine = create_engine(url)
    root = Path(__file__).resolve().parents[2]
    migration = Config(str(root / "apps/api/alembic.ini"))
    migration.set_main_option("script_location", str(root / "apps/api/alembic"))
    migration.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    # Require a fresh database; never drop an existing application's tables.
    try:
        if inspect(engine).get_table_names():
            pytest.fail("Refusing setup: mails_test database must be empty")
        command.upgrade(migration, "head")
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
        try:
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
