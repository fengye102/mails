import base64
import os

import pytest
from fake_imap import FakeImapServer
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from icloud_api.app import create_app
from icloud_api.hme_client import AccountInfo
from icloud_api.models import Base
from icloud_api.settings import Settings


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
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    # 规格要求：SQLite 测试必须开外键约束，否则级联/引用错误测不出来。
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):  # pragma: no cover - 驱动回调
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


class FakeCookieValidator:
    """假上游：记录收到的 Cookie，返回合并后的 Cookie 与账号身份。"""

    def __init__(self, *, merged: dict[str, str] | None = None, error: Exception | None = None) -> None:
        self.merged = merged
        self.error = error
        self.seen: list[dict[str, str]] = []

    async def __call__(self, cookies: dict[str, str], account) -> tuple[dict[str, str], AccountInfo]:
        self.seen.append(dict(cookies))
        if self.error is not None:
            raise self.error
        merged = dict(cookies)
        if self.merged:
            merged.update(self.merged)
        return merged, AccountInfo(dsid="123456", apple_id="user@example.com", primary_email="alias@icloud.com")


@pytest.fixture
def validator() -> FakeCookieValidator:
    return FakeCookieValidator(merged={"X-APPLE-WEBAUTH-TOKEN": "refreshed"})


@pytest.fixture
def client(settings: Settings, engine, validator: FakeCookieValidator):
    from fastapi.testclient import TestClient

    app = create_app(settings, engine, cookie_validator=validator)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fake_imap_factory():
    """注入假 IMAP 服务器的客户端工厂：构造出的客户端连本地假服务器。

    用法：`factory = fake_imap_factory(server)`；server 用 accept_any_login=True，
    或让 username/password 与测试账号的 icloud_email/App Password 一致。
    """
    from icloud_api.accounts import account_app_password
    from icloud_api.mail import service as mail_service
    from icloud_api.mail.imap_client import ImapClient

    def factory_for(server: FakeImapServer):
        def _factory(account, key, **overrides):
            # 测试专用工厂：凭据与真实工厂同源（icloud_email/real_email + 解密后的
            # App Password），只是把连接换成本地假服务器。
            username = mail_service.imap_login_email(account)
            app_password = account_app_password(account, key)
            options = {
                "host": "127.0.0.1",
                "port": 0,
                "username": username,
                "app_password": app_password,
                "connector": server.connect,
            }
            options.update(overrides)
            return ImapClient(**options)

        return _factory

    return factory_for
