import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest
from mails_api.models import Base, Mailbox, Message, ProviderConnection, SyncRun
from mails_api.providers.base import (
    MessageListIssue,
    MessageListResult,
    MessageSummary,
    NormalizedMessage,
    ProviderError,
)
from mails_api.security.crypto import encrypt_secret
from mails_api.services.sync import sync_mailbox
from sqlalchemy import func, select
from sqlalchemy.orm import Session

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
KEY = bytes(range(32))


class FakeAdapter:
    def __init__(self):
        self.summaries = [
            MessageSummary("one", "graph", "sender", "Code", "preview", NOW)
        ]
        self.issues = []
        self.detail_calls = 0
        self.failure = None
        self.bad_ids = set()
        self.entered = None
        self.release = None
        self.secrets = []

    async def list_messages(self, url, secret, address):
        self.secrets.append(secret)
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.failure:
            raise self.failure
        return MessageListResult(self.summaries, self.issues)

    async def get_message(self, url, secret, address, summary):
        self.detail_calls += 1
        if summary.provider_id in self.bad_ids:
            raise ValueError("credential-private body-private")
        return NormalizedMessage(
            **asdict(summary),
            recipient=address,
            body_text="verification code: 123456",
            rfc_message_id=None,
        )


@pytest.fixture
def db(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def mailbox(db):
    connection = ProviderConnection(
        kind="outlook_plus",
        name="test",
        base_url="https://mail.example",
        encrypted_secret=encrypt_secret(KEY, "credential-private"),
        status="active",
    )
    db.add(connection)
    db.flush()
    item = Mailbox(
        connection_id=connection.id,
        provider_mailbox_key="test",
        address="test@example.com",
        sync_state="idle",
    )
    db.add(item)
    db.commit()
    return item


@pytest.mark.asyncio
async def test_repeat_sync_is_idempotent(db, mailbox):
    adapter = FakeAdapter()
    first = await sync_mailbox(db, mailbox.id, adapter, NOW, encryption_key=KEY)
    second = await sync_mailbox(db, mailbox.id, adapter, NOW)
    assert first.status == second.status == "success"
    assert second.new_message_count == 0
    assert db.scalar(select(func.count(Message.id))) == 1
    assert adapter.detail_calls == 1
    assert adapter.secrets[0] == "credential-private"
    assert db.scalar(select(Message)).verification_code == "123456"


@pytest.mark.asyncio
async def test_partial_keeps_good_summary_and_detail(db, mailbox):
    adapter = FakeAdapter()
    adapter.issues = [MessageListIssue(0, "UPSTREAM_CONTRACT")]
    adapter.summaries.insert(0, replace(adapter.summaries[0], provider_id="bad"))
    adapter.bad_ids.add("bad")
    run = await sync_mailbox(db, mailbox.id, adapter, NOW)
    assert run.status == "partial"
    assert run.new_message_count == 1
    assert run.error_code == "UPSTREAM_CONTRACT"
    assert "private" not in run.error_summary
    assert db.scalar(select(func.count(Message.id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code",
    [
        (ProviderError("UPSTREAM_AUTH"), "UPSTREAM_AUTH"),
        (TimeoutError("credential-private"), "UPSTREAM_TIMEOUT"),
    ],
)
async def test_failed_mailbox_does_not_affect_other(db, mailbox, error, code):
    other = Mailbox(
        connection_id=mailbox.connection_id,
        provider_mailbox_key="other",
        address="other@example.com",
        sync_state="idle",
    )
    db.add(other)
    db.commit()
    bad = FakeAdapter()
    bad.failure = error
    failed = await sync_mailbox(db, mailbox.id, bad, NOW)
    good = await sync_mailbox(db, other.id, FakeAdapter(), NOW)
    assert failed.status == "failed" and failed.error_code == code
    assert good.status == "success"
    assert "private" not in failed.error_summary


@pytest.mark.asyncio
async def test_concurrent_trigger_returns_current_without_queue(db, mailbox, engine):
    adapter = FakeAdapter()
    adapter.entered, adapter.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(sync_mailbox(db, mailbox.id, adapter, NOW))
    await adapter.entered.wait()
    with Session(engine) as second_db:
        current = await asyncio.wait_for(
            sync_mailbox(second_db, mailbox.id, adapter, NOW), 1
        )
        assert current.status == "running"
        current_id = current.id
    adapter.release.set()
    completed = await task
    assert completed.id == current_id
    assert db.scalar(select(func.count(SyncRun.id))) == 1
    assert adapter.detail_calls == 1


@pytest.mark.asyncio
async def test_first_sync_age_filter_and_duplicate_batch(db, mailbox):
    adapter = FakeAdapter()
    old = replace(
        adapter.summaries[0], provider_id="old", received_at=NOW - timedelta(days=8)
    )
    adapter.summaries += [old, adapter.summaries[0]]
    first = await sync_mailbox(db, mailbox.id, adapter, NOW)
    assert first.new_message_count == 1
    second = await sync_mailbox(db, mailbox.id, adapter, NOW)
    assert second.new_message_count == 1
    assert db.scalar(select(func.count(Message.id))) == 2


@pytest.mark.asyncio
async def test_cancelled_sync_releases_lock_and_finishes_run(db, mailbox):
    adapter = FakeAdapter()
    adapter.entered, adapter.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(sync_mailbox(db, mailbox.id, adapter, NOW))
    await adapter.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = db.scalar(select(SyncRun))
    assert run.status == "failed" and run.finished_at is not None
    assert (await sync_mailbox(db, mailbox.id, FakeAdapter(), NOW)).status == "success"


def test_sync_routes_auth_csrf_and_results(engine):
    import base64

    import httpx
    from fastapi.testclient import TestClient
    from mails_api.app import create_app
    from mails_api.models import User
    from mails_api.security.passwords import hash_password
    from mails_api.settings import Settings

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(username="owner", password_hash=hash_password("secret")))
        connection = ProviderConnection(
            kind="outlook_plus",
            name="test",
            base_url="https://mail.example",
            encrypted_secret=encrypt_secret(KEY, "credential-private"),
            status="active",
        )
        session.add(connection)
        session.flush()
        item = Mailbox(
            connection_id=connection.id,
            provider_mailbox_key="test",
            address="test@example.com",
            sync_state="idle",
        )
        session.add(item)
        session.commit()
        mailbox_id = str(item.id)
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.headers["X-API-Key"] == "credential-private"
        return httpx.Response(401, text="credential-private body-private")

    settings = Settings(
        database_url="sqlite://",
        encryption_key=base64.b64encode(KEY).decode(),
        public_origin="http://localhost:5173",
        upstream_origins=["https://mail.example"],
    )
    with TestClient(
        create_app(
            settings, engine, httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        )
    ) as client:
        path = f"/api/v1/mailboxes/{mailbox_id}/sync"
        assert client.post(path).status_code == 401
        assert client.get("/api/v1/sync-runs").status_code == 401
        login = client.post(
            "/api/v1/session",
            json={"username": "owner", "password": "secret"},
            headers={"Origin": settings.public_origin},
        )
        assert client.post(path).status_code == 403
        response = client.post(
            path, headers={"X-CSRF-Token": login.json()["csrf_token"]}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "failed"
        assert response.json()["error_code"] == "UPSTREAM_AUTH"
        assert "private" not in response.text
        assert client.get(
            "/api/v1/sync-runs", params={"mailbox_id": mailbox_id}
        ).json() == [response.json()]
        assert (
            client.get(
                "/api/v1/sync-runs",
                params={"mailbox_id": "00000000-0000-0000-0000-000000000000"},
            ).json()
            == []
        )
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_periodic_loop_runs_after_interval_and_cancels(monkeypatch):
    from types import SimpleNamespace

    import mails_api.services.sync as module
    from mails_api.app import periodic_sync

    ticks = []
    completed = asyncio.Event()

    async def cycle(app):
        ticks.append(app)
        completed.set()

    monkeypatch.setattr(module, "sync_all_mailboxes", cycle)
    app = SimpleNamespace()
    task = asyncio.create_task(periodic_sync(app, interval=0.01))
    assert not ticks
    await asyncio.wait_for(completed.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ticks == [app]


@pytest.mark.asyncio
async def test_scheduler_mailboxes_are_concurrent_and_decrypt_secrets(
    db, mailbox, engine
):
    import base64
    from types import SimpleNamespace

    import httpx
    from mails_api.services.sync import sync_all_mailboxes
    from mails_api.settings import Settings

    other = Mailbox(
        connection_id=mailbox.connection_id,
        provider_mailbox_key="other",
        address="other@example.com",
        sync_state="idle",
    )
    db.add(other)
    db.commit()
    good_entered = asyncio.Event()

    async def upstream(request):
        assert request.headers["X-API-Key"] == "credential-private"
        if request.url.params["email"] == mailbox.address:
            await asyncio.wait_for(good_entered.wait(), 1)
            return httpx.Response(401)
        good_entered.set()
        return httpx.Response(
            200, json={"success": True, "code": "OK", "data": {"emails": []}}
        )

    settings = Settings(
        database_url="sqlite://",
        encryption_key=base64.b64encode(KEY).decode(),
        public_origin="http://localhost:5173",
        upstream_origins=["https://mail.example"],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        app = SimpleNamespace(
            state=SimpleNamespace(
                engine=engine, settings=settings, upstream_client=client
            )
        )
        await sync_all_mailboxes(app)
    db.expire_all()
    assert db.get(Mailbox, mailbox.id).sync_state == "failed"
    assert db.get(Mailbox, other.id).sync_state == "success"
    assert db.scalar(select(func.count(SyncRun.id))) == 2


@pytest.mark.asyncio
async def test_real_adapter_without_key_never_sends_request(db, mailbox):
    import httpx
    from mails_api.providers.outlook_plus import OutlookPlusAdapter

    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as client:
        run = await sync_mailbox(db, mailbox.id, OutlookPlusAdapter(client), NOW)
    assert run.status == "failed"
    assert run.error_code == "CONNECTION_SECRET_INVALID"
    assert calls == []


@pytest.mark.asyncio
async def test_small_pool_released_during_http_and_scheduler_is_bounded(tmp_path):
    import base64
    from types import SimpleNamespace

    import httpx
    from mails_api.services.sync import sync_all_mailboxes
    from mails_api.settings import Settings
    from sqlalchemy import create_engine

    engine = create_engine(
        "sqlite:///" + str(tmp_path / "limited-pool.db"),
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        connection = ProviderConnection(
            kind="outlook_plus",
            name="test",
            base_url="https://mail.example",
            encrypted_secret=encrypt_secret(KEY, "secret"),
            status="active",
        )
        db.add(connection)
        db.flush()
        for index in range(8):
            db.add(
                Mailbox(
                    connection_id=connection.id,
                    provider_mailbox_key=str(index),
                    address=f"box{index}@example.com",
                    sync_state="idle",
                )
            )
        db.commit()
    active = peak = ticks = 0
    checked_out = []
    pulse_counts = []

    async def heartbeat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.001)

    async def upstream(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        checked_out.append(engine.pool.checkedout())
        before = ticks
        await asyncio.sleep(0.01)
        pulse_counts.append(ticks - before)
        active -= 1
        item = {
            "id": "one",
            "method": "graph",
            "from_address": "sender",
            "subject": "Code",
            "content_preview": "",
            "timestamp": datetime.now(timezone.utc).timestamp(),
        }
        if request.url.path.endswith("/messages"):
            data = {"emails": [item]}
        else:
            data = {
                **item,
                "to_address": request.url.params["email"],
                "content": "Code: 123456",
                "raw_content": "",
            }
        return httpx.Response(200, json={"success": True, "code": "OK", "data": data})

    settings = Settings(
        database_url="sqlite://",
        encryption_key=base64.b64encode(KEY).decode(),
        public_origin="http://localhost:5173",
        upstream_origins=["https://mail.example"],
    )
    pulse = asyncio.create_task(heartbeat())
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            app = SimpleNamespace(
                state=SimpleNamespace(
                    engine=engine, settings=settings, upstream_client=client
                )
            )
            await sync_all_mailboxes(app)
        with Session(engine) as db:
            runs = list(db.scalars(select(SyncRun)))
            assert len(runs) == 8
            assert all(run.status == "success" for run in runs)
            assert db.scalar(select(func.count(Message.id))) == 8
        assert checked_out and set(checked_out) == {0}
        assert 1 < peak <= 4
        assert all(count > 0 for count in pulse_counts)
    finally:
        pulse.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pulse
        engine.dispose()
