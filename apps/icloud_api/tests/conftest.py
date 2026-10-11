import base64
import os

import pytest
from icloud_api.app import create_app
from icloud_api.settings import Settings
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
def data_key() -> bytes:
    return base64.b64decode(base64.b64encode(os.urandom(32)))


@pytest.fixture
def settings(data_key: bytes) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        operator_token="operator-test-token",
        data_key=base64.b64encode(data_key).decode("ascii"),
    )


@pytest.fixture
def engine():
    # 内存 SQLite 必须共用一条连接，否则每个 Session 看到的是空库。
    return create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


@pytest.fixture
def client(settings: Settings, engine):
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings, engine)) as test_client:
        yield test_client
