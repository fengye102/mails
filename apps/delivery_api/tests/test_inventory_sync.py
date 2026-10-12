"""C 站库存同步：引擎客户端契约、库存对齐规则、同步接口。

三层分开测，因为每层坏掉的表现不同：

- **客户端**：形状/分页/错误码错了 → 要么把「拉不到」当「上游空了」，
  要么把别人的别名当自己的库存；
- **对齐逻辑**：规则错了 → 正在取件的邮箱被同步打掉（客户取件失败），
  或者已失效的邮箱继续被分配出去（客户等一封不会来的邮件）；
- **接口**：漏了运营鉴权或凭据进了响应体 → 事故。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from delivery_api.icloud_engine import AliasSnapshot, EngineError, ICloudEngineClient
from delivery_api.inventory_sync import DEFAULT_SOURCE_LABEL, apply_alias_snapshot
from delivery_api.models import (
    Base,
    DeliveryAllocation,
    InventoryMailbox,
)

ENGINE_URL = "https://engine.example"
TOKEN = "operator-token-abc"
ACCOUNT = "acct-1"
NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)


def alias(address: str, active: bool = True) -> dict:
    return {
        "address": address,
        "anonymous_id": f"anon-{address}",
        "label": "",
        "active": active,
        "created_at": "2026-10-12T07:00:00Z",
        "forward_to_email": "",
    }


def engine_payload(aliases: list[dict], *, offset: int = 0, limit: int = 1000) -> dict:
    return {
        "account_id": ACCOUNT,
        "count": len(aliases),
        "total": len(aliases),
        "limit": limit,
        "offset": offset,
        "synced": {"added": 0, "updated": 0, "missing": 0},
        "aliases": aliases,
    }


def client_for(handler, seen: list | None = None) -> ICloudEngineClient:
    def upstream(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    return ICloudEngineClient(
        httpx.Client(transport=httpx.MockTransport(upstream)), ENGINE_URL, TOKEN
    )


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def add_mailbox(db: Session, address: str, status: str = "available", source_label: str | None = None):
    row = InventoryMailbox(
        address=address,
        source_label=source_label or DEFAULT_SOURCE_LABEL,
        status=status,
    )
    db.add(row)
    db.commit()
    return row


def statuses(db: Session) -> dict[str, str]:
    return {
        row.address: row.status
        for row in db.scalars(select(InventoryMailbox)).all()
    }


# ---- 引擎客户端 ----


def test_client_pages_until_short_page(monkeypatch):
    monkeypatch.setattr("delivery_api.icloud_engine._ALIAS_PAGE", 2)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.path == f"/icloud-api/v1/accounts/{ACCOUNT}/aliases"
        offset = int(request.url.params["offset"])
        pages = {0: [alias("a@icloud.com"), alias("b@icloud.com")], 2: [alias("c@icloud.com")]}
        return httpx.Response(200, json=engine_payload(pages[offset], offset=offset, limit=2))

    snapshots = client_for(handler, seen).list_aliases(ACCOUNT)

    assert [s.address for s in snapshots] == ["a@icloud.com", "b@icloud.com", "c@icloud.com"]
    assert [request.url.params["offset"] for request in seen] == ["0", "2"]
    assert snapshots[0].active is True


def test_client_deduplicates_repeated_addresses(monkeypatch):
    monkeypatch.setattr("delivery_api.icloud_engine._ALIAS_PAGE", 2)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # 请求记录由 client_for 的包装统一负责，这里不要再记一遍（否则每条记两次）。
        offset = int(request.url.params["offset"])
        # 首页两条里有一条重复地址，第二页给一条短页结束翻页。
        pages = {
            0: [alias("a@icloud.com"), alias("a@icloud.com", active=False)],
            2: [alias("b@icloud.com")],
        }
        return httpx.Response(200, json=engine_payload(pages[offset], offset=offset, limit=2))

    snapshots = client_for(handler, seen).list_aliases(ACCOUNT)

    assert [(s.address, s.active) for s in snapshots] == [
        ("a@icloud.com", True),  # 同地址只保留第一次
        ("b@icloud.com", True),
    ]
    assert [request.url.params["offset"] for request in seen] == ["0", "2"]


def test_client_gives_up_when_upstream_ignores_paging(monkeypatch):
    """上游反复返回同一页（offset 被忽略）时必须报错，不能无限翻。"""
    monkeypatch.setattr("delivery_api.icloud_engine._ALIAS_PAGE", 2)
    monkeypatch.setattr("delivery_api.icloud_engine._MAX_ALIASES", 6)
    page = engine_payload([alias("a@icloud.com"), alias("a@icloud.com")], limit=2)

    client = client_for(lambda request: httpx.Response(200, json=page))
    with pytest.raises(EngineError) as excinfo:
        client.list_aliases(ACCOUNT)
    assert excinfo.value.code == "UPSTREAM_CONTRACT"


@pytest.mark.parametrize(
    "payload",
    [
        {"aliases": "not-a-list"},
        {"aliases": [{"address": "", "active": True}]},
        {"aliases": [{"active": True}]},
        {"aliases": [{"address": "a@icloud.com", "active": "yes"}]},
        {"aliases": [["a@icloud.com", True]]},
        [],
    ],
)
def test_client_rejects_malformed_alias_payloads(payload):
    # 形状不对一律当契约错：不猜默认值，否则会把「没解析出来」当成「上游没有别名」。
    client = client_for(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(EngineError) as excinfo:
        client.list_aliases(ACCOUNT)
    assert excinfo.value.code == "UPSTREAM_CONTRACT"


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "UPSTREAM_AUTH"), (403, "UPSTREAM_AUTH"), (503, "UPSTREAM_UNAVAILABLE"), (404, "UPSTREAM_CONTRACT")],
)
def test_client_maps_status_codes(status, code):
    client = client_for(lambda request: httpx.Response(status, json={}))
    with pytest.raises(EngineError) as excinfo:
        client.list_aliases(ACCOUNT)
    assert excinfo.value.code == code
    assert TOKEN not in str(excinfo.value)


def test_client_maps_timeout_without_leaking_token():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    with pytest.raises(EngineError) as excinfo:
        client_for(handler).list_aliases(ACCOUNT)
    assert excinfo.value.code == "UPSTREAM_TIMEOUT"
    assert TOKEN not in str(excinfo.value)


# ---- 库存对齐规则 ----


def test_new_upstream_aliases_become_inventory(db):
    outcome = apply_alias_snapshot(
        db,
        [AliasSnapshot("a@icloud.com", True), AliasSnapshot("b@icloud.com", False)],
        now=NOW,
    )
    db.commit()

    assert outcome.as_dict() == {
        "upstream_total": 2,
        "added": 2,
        "reactivated": 0,
        "deactivated": 0,
        "missing": 0,
        "protected": 0,
        "ambiguous": 0,
        "disabled_kept": 0,
    }
    assert statuses(db) == {"a@icloud.com": "available", "b@icloud.com": "inactive"}


def test_sync_is_idempotent(db):
    snapshots = [AliasSnapshot("a@icloud.com", True)]
    apply_alias_snapshot(db, snapshots, now=NOW)
    db.commit()
    second = apply_alias_snapshot(db, snapshots, now=NOW)
    db.commit()

    assert second.added == 0
    assert db.scalar(select(func.count(InventoryMailbox.id))) == 1


def test_address_case_and_whitespace_do_not_create_duplicates(db):
    add_mailbox(db, "Alias@iCloud.com")
    outcome = apply_alias_snapshot(db, [AliasSnapshot("  alias@icloud.com ", True)], now=NOW)
    db.commit()

    assert outcome.added == 0
    assert db.scalar(select(func.count(InventoryMailbox.id))) == 1


def test_upstream_inactive_deactivates_row(db):
    add_mailbox(db, "a@icloud.com")
    outcome = apply_alias_snapshot(db, [AliasSnapshot("a@icloud.com", False)], now=NOW)
    db.commit()

    assert outcome.deactivated == 1
    assert statuses(db) == {"a@icloud.com": "inactive"}


def test_upstream_reappearing_reactivates_missing_row(db):
    add_mailbox(db, "a@icloud.com", status="missing")
    outcome = apply_alias_snapshot(db, [AliasSnapshot("a@icloud.com", True)], now=NOW)
    db.commit()

    assert outcome.reactivated == 1
    assert statuses(db) == {"a@icloud.com": "available"}


def test_vanished_alias_is_marked_missing_never_deleted(db):
    row = add_mailbox(db, "gone@icloud.com")
    outcome = apply_alias_snapshot(db, [AliasSnapshot("keep@icloud.com", True)], now=NOW)
    db.commit()

    assert outcome.missing == 1
    assert db.get(InventoryMailbox, row.id) is not None, "幽灵行只能标记，不能删"
    assert statuses(db)["gone@icloud.com"] == "missing"


def test_alias_with_active_allocation_is_protected(db):
    row = add_mailbox(db, "busy@icloud.com", status="allocated")
    db.add(
        DeliveryAllocation(
            mailbox_id=row.id,
            external_ref="order-1",
            status="active",
            allocated_at=NOW - timedelta(minutes=5),
            expires_at=NOW + timedelta(hours=1),
        )
    )
    db.commit()

    outcome = apply_alias_snapshot(db, [], now=NOW)
    db.commit()

    assert outcome.protected == 1
    assert outcome.missing == 0
    assert statuses(db) == {"busy@icloud.com": "allocated"}
    # 行没被动：仍然挂着它的分配。
    assert db.scalar(select(func.count(DeliveryAllocation.id))) == 1


def test_allocated_row_with_expired_allocation_becomes_missing(db):
    row = add_mailbox(db, "stale@icloud.com", status="allocated")
    db.add(
        DeliveryAllocation(
            mailbox_id=row.id,
            external_ref="order-2",
            status="active",
            allocated_at=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(hours=1),
        )
    )
    db.commit()

    outcome = apply_alias_snapshot(db, [], now=NOW)
    db.commit()

    assert outcome.protected == 0
    assert outcome.missing == 1
    assert statuses(db) == {"stale@icloud.com": "missing"}


def test_disabled_row_is_left_to_the_operator(db):
    add_mailbox(db, "off@icloud.com", status="disabled")
    outcome = apply_alias_snapshot(db, [AliasSnapshot("off@icloud.com", True)], now=NOW)
    db.commit()

    assert outcome.disabled_kept == 1
    assert statuses(db) == {"off@icloud.com": "disabled"}


def test_duplicate_local_rows_are_reported_and_left_alone(db):
    add_mailbox(db, "dup@icloud.com")
    add_mailbox(db, "dup@icloud.com")
    outcome = apply_alias_snapshot(db, [AliasSnapshot("dup@icloud.com", True)], now=NOW)
    db.commit()

    assert outcome.ambiguous == 1
    assert outcome.added == 0
    assert db.scalar(select(func.count(InventoryMailbox.id))) == 2


def test_sync_only_touches_its_own_source_label(db):
    add_mailbox(db, "shared@icloud.com", source_label="manual")
    outcome = apply_alias_snapshot(db, [AliasSnapshot("shared@icloud.com", True)], now=NOW)
    db.commit()

    rows = db.scalars(select(InventoryMailbox).order_by(InventoryMailbox.source_label)).all()
    assert outcome.added == 1
    assert [(row.source_label, row.status) for row in rows] == [
        (DEFAULT_SOURCE_LABEL, "available"),
        ("manual", "available"),
    ]


# ---- 同步接口 ----


def make_sync_client(make_client, upstream, **overrides):
    options = {
        "icloud_engine_url": ENGINE_URL,
        "icloud_engine_token": TOKEN,
        "icloud_engine_account_id": ACCOUNT,
    }
    options.update(overrides)
    test_client = make_client(**options)
    test_client.app.state.icloud_engine_client = httpx.Client(
        transport=httpx.MockTransport(upstream)
    )
    return test_client


def test_sync_requires_operator_token(make_client):
    with make_client(
        icloud_engine_url=ENGINE_URL, icloud_engine_token=TOKEN, icloud_engine_account_id=ACCOUNT
    ) as client:
        assert client.post("/delivery-api/v1/inventory/sync", json={}).status_code == 401


def test_sync_reports_unconfigured_engine(make_client, operator_headers):
    with make_client() as client:
        response = client.post("/delivery-api/v1/inventory/sync", headers=operator_headers)
        assert response.status_code == 409
        assert response.json()["detail"] == {"code": "DELIVERY_ENGINE_NOT_CONFIGURED"}


def test_sync_pulls_snapshot_and_reports_counts(make_client, operator_headers):
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(
            200,
            json=engine_payload([alias("a@icloud.com"), alias("b@icloud.com", active=False)]),
        )

    with make_sync_client(make_client, upstream) as client:
        response = client.post(
            "/delivery-api/v1/inventory/sync", headers=operator_headers, json={}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["account_id"] == ACCOUNT
        assert body["source_label"] == DEFAULT_SOURCE_LABEL
        assert body["upstream_total"] == 2
        assert body["added"] == 2
        assert TOKEN not in response.text

        with Session(client.app.state.engine) as db:
            assert {
                row.address: row.status
                for row in db.scalars(select(InventoryMailbox)).all()
            } == {"a@icloud.com": "available", "b@icloud.com": "inactive"}

        # 幂等：再同步一次不会重复入库。
        second = client.post("/delivery-api/v1/inventory/sync", headers=operator_headers, json={})
        assert second.json()["added"] == 0


def test_sync_maps_engine_auth_failure_to_502(make_client, operator_headers):
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "token 无效"})

    with make_sync_client(make_client, upstream) as client:
        response = client.post(
            "/delivery-api/v1/inventory/sync", headers=operator_headers, json={}
        )
        assert response.status_code == 502
        assert response.json()["detail"] == {"code": "UPSTREAM_AUTH"}
        assert TOKEN not in response.text


def test_sync_writes_an_audit_event_without_credentials(make_client, operator_headers):
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=engine_payload([alias("a@icloud.com")]))

    with make_sync_client(make_client, upstream) as client:
        client.post("/delivery-api/v1/inventory/sync", headers=operator_headers, json={})
        audit = client.get("/delivery-api/v1/inventory/audit", headers=operator_headers).json()
        event = next(item for item in audit["items"] if item["event_type"] == "inventory_synced")
        assert event["event_type"] == "inventory_synced"
        assert TOKEN not in str(audit)


def test_sync_body_can_override_account_and_source_label(make_client, operator_headers):
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=engine_payload([]))

    with make_sync_client(make_client, upstream) as client:
        response = client.post(
            "/delivery-api/v1/inventory/sync",
            headers=operator_headers,
            json={"account_id": "acct-9", "source_label": "pool-a"},
        )
        assert response.status_code == 200
        assert response.json()["account_id"] == "acct-9"
        assert response.json()["source_label"] == "pool-a"
        assert seen[0].url.path == "/icloud-api/v1/accounts/acct-9/aliases"
