import pytest
from fastapi.testclient import TestClient
from mails_api.app import create_app
from mails_api.models import Base, User
from mails_api.security.passwords import hash_password
from mails_api.settings import Settings
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.pool import StaticPool


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    yield engine
    engine.dispose()


@pytest.fixture
def security_client(engine):
    Base.metadata.create_all(engine)
    with DbSession(engine) as db:
        db.add(User(username="owner", password_hash=hash_password("secret")))
        db.commit()
    settings = Settings(
        database_url="sqlite://",
        encryption_key="test",
        public_origin="http://localhost:5173",
        upstream_origins=[],
    )
    with TestClient(create_app(settings=settings, engine=engine)) as client:
        yield client
