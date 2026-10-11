"""C 测试共用夹具：内存 SQLite + 外键校验开启（与 PostgreSQL 行为一致）。"""

import pytest
from delivery_api.app import create_app
from delivery_api.models import Base
from delivery_api.settings import Settings
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

OPERATOR = "operator-test-token"


@pytest.fixture
def make_client():
    """按需构造带自定义配置的客户端；默认配置与现网一致。"""

    def factory(**overrides):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        # SQLite 默认不校验外键，会让「审计事件先于被引用行插入」这类顺序问题漏过测试；
        # 打开后与 PostgreSQL 行为一致。
        @event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(engine)
        options = {
            "database_url": "sqlite+pysqlite:///:memory:",
            "operator_token": OPERATOR,
            "encryption_key": "test-encryption-key",
            "pickup_ttl_seconds": 300,
        }
        options.update(overrides)
        app = create_app(Settings(**options), engine=engine)
        return TestClient(app)

    return factory


@pytest.fixture
def client(make_client):
    with make_client() as test_client:
        yield test_client


@pytest.fixture
def operator_headers():
    return {"Authorization": f"Bearer {OPERATOR}"}
