"""E1：池密钥、鉴权、限流、可用性判据与单邮箱运维接管。

这里断言的**对外形状**就是「注册机零改动」的护栏：信封固定四个键、错误码大写、
`NO_AVAILABLE_ACCOUNT` 之类是 200+success=false。谁把 `{"detail": ...}` 漏出去，
或者把空池改成 404，测试就该红。
"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from delivery_api import pool
from delivery_api.models import (
    DeliveryAuditEvent,
    InventoryMailbox,
    PoolApiKey,
    PoolClaim,
    utc_now,
)
from delivery_api.security import hash_secret
from delivery_api.settings import Settings

OPERATOR = "operator-test-token"
ADMIN = {"Authorization": f"Bearer {OPERATOR}"}


def create_key(client, **overrides) -> dict:
    payload = {"name": "注册机", **overrides}
    response = client.post("/delivery-api/v1/pool/keys", headers=ADMIN, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def bearer(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def seed_mailbox(
    client,
    *,
    address: str = "a@plus.test",
    source_label: str = "outlook_plus",
    status: str = "available",
    pool_status: str | None = None,
    expires_at=None,
) -> str:
    """登记一个库存邮箱；需要造异常态时直接改行（走接口造不出这些组合）。"""

    created = client.post(
        "/delivery-api/v1/inventory/mailboxes",
        headers=ADMIN,
        json={"address": address, "source_label": source_label, "messages": []},
    )
    assert created.status_code == 201, created.text
    mailbox_id = created.json()["id"]
    if pool_status is not None or status != "available" or expires_at is not None:
        with Session(client.app.state.engine) as db:
            row = db.get(InventoryMailbox, mailbox_id)
            assert row is not None
            if pool_status is not None:
                row.pool_status = pool_status
            row.status = status
            row.expires_at = expires_at
            db.commit()
    return mailbox_id


def hold_claim(client, mailbox_id: str, *, token: str = "claim-token-1", status: str = "claimed") -> None:
    now = utc_now()
    with Session(client.app.state.engine) as db:
        row = db.get(InventoryMailbox, mailbox_id)
        assert row is not None
        row.pool_status = pool.POOL_CLAIMED
        row.pool_lease_expires_at = now + timedelta(minutes=5)
        db.add(
            PoolClaim(
                mailbox_id=mailbox_id,
                caller_id="caller-1",
                task_id="task-1",
                project="proj-a",
                claim_token=token,
                status=status,
                claimed_at=now,
                lease_expires_at=now + timedelta(minutes=5),
            )
        )
        db.commit()


# ------------------------------------------------------------------ 池密钥


def test_created_key_plaintext_returned_once_and_only_hash_stored(client):
    created = create_key(client)
    raw = created["api_key"]
    assert raw.startswith(pool.POOL_KEY_PREFIX)
    assert "key_hash" not in created

    with Session(client.app.state.engine) as db:
        row = db.scalar(select(PoolApiKey))
        assert row is not None
        assert row.key_hash == hash_secret(raw)
        # 明文不许以任何形式落库：整行扫一遍（含 JSON 列）。
        whole_row = {c.name: getattr(row, c.name) for c in PoolApiKey.__table__.columns}
        assert raw not in str(whole_row)

    listed = client.get("/delivery-api/v1/pool/keys", headers=ADMIN).json()["items"]
    assert len(listed) == 1
    assert "key_hash" not in listed[0]
    assert raw not in str(listed)


def test_created_key_response_is_not_cacheable(client):
    created = create_key(client)
    assert client.get("/delivery-api/v1/pool/keys", headers=ADMIN).status_code == 200
    raw = created["api_key"]
    response = client.get("/pool-api/v1/stats", headers=bearer(raw))
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_update_key_can_disable_and_reshape_scope(client):
    created = create_key(client)
    response = client.patch(
        f"/delivery-api/v1/pool/keys/{created['id']}",
        headers=ADMIN,
        json={"enabled": False, "allowed_domains": ["Plus.Test", "plus.test", " "], "rate_limit_per_minute": 7},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["allowed_domains"] == ["plus.test"]  # 去重 + 归一化小写 + 丢空串
    assert body["rate_limit_per_minute"] == 7
    assert client.patch("/delivery-api/v1/pool/keys/nope", headers=ADMIN, json={"enabled": True}).status_code == 404


# ------------------------------------------------------------------ 鉴权与信封


@pytest.mark.parametrize("style", ["bearer", "x-api-key"])
def test_key_authenticates_with_both_headers(client, style):
    raw = create_key(client)["api_key"]
    headers = bearer(raw) if style == "bearer" else {"X-API-Key": raw}
    response = client.get("/pool-api/v1/stats", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"success", "code", "message", "data"}
    assert body["success"] is True
    assert body["code"] == "OK"
    assert body["message"] == "success"
    assert set(body["data"]["pool_counts"]) == set(pool.POOL_STATUSES)


def test_pool_key_routes_do_not_require_operator_token(client):
    raw = create_key(client)["api_key"]
    assert client.get("/pool-api/v1/stats", headers=bearer(raw)).status_code == 200
    assert client.get("/delivery-api/v1/pool/keys").status_code == 401


def test_missing_key_is_401_with_oep_envelope(client):
    create_key(client)
    response = client.get("/pool-api/v1/stats")
    assert response.status_code == 401
    assert response.json() == {
        "success": False,
        "code": "UNAUTHORIZED",
        "message": "API Key 缺失或无效",
        "data": None,
    }


def test_unknown_and_disabled_key_share_the_same_code(client):
    created = create_key(client)
    unknown = client.get("/pool-api/v1/stats", headers=bearer(pool.POOL_KEY_PREFIX + "x" * 43))
    assert unknown.status_code == 401
    assert unknown.json()["code"] == "UNAUTHORIZED"

    client.patch(f"/delivery-api/v1/pool/keys/{created['id']}", headers=ADMIN, json={"enabled": False})
    disabled = client.get("/pool-api/v1/stats", headers=bearer(created["api_key"]))
    assert disabled.status_code == 401
    # 不向外区分「Key 存在但被停用」和「Key 不存在」。
    assert disabled.json()["code"] == unknown.json()["code"]


def test_pool_without_any_key_reports_not_configured(client):
    response = client.get("/pool-api/v1/stats", headers=bearer(pool.POOL_KEY_PREFIX + "y" * 43))
    assert response.status_code == 403
    assert response.json()["code"] == "API_KEY_NOT_CONFIGURED"


def test_pool_disabled_returns_feature_disabled(client):
    raw = create_key(client)["api_key"]
    client.app.state.settings.pool_enabled = False
    response = client.get("/pool-api/v1/stats", headers=bearer(raw))
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "FEATURE_DISABLED"
    assert body["data"] == {"feature": "external_pool"}


def test_rate_limit_returns_429_rate_limited(client):
    raw = create_key(client, rate_limit_per_minute=2)["api_key"]
    headers = bearer(raw)
    assert client.get("/pool-api/v1/stats", headers=headers).status_code == 200
    assert client.get("/pool-api/v1/stats", headers=headers).status_code == 200
    blocked = client.get("/pool-api/v1/stats", headers=headers)
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "RATE_LIMITED"


def test_rate_limiter_slides_and_treats_zero_as_unlimited():
    limiter = pool.RateLimiter(window_seconds=60)
    assert limiter.allow("k", 1, now=0.0) is True
    assert limiter.allow("k", 1, now=1.0) is False
    assert limiter.allow("k", 1, now=61.0) is True
    assert limiter.allow("other", 0, now=0.0) is True
    assert limiter.allow("other", 0, now=0.0) is True


def test_rate_limit_is_per_key(client):
    first = create_key(client, name="甲", rate_limit_per_minute=1)["api_key"]
    second = create_key(client, name="乙", rate_limit_per_minute=1)["api_key"]
    assert client.get("/pool-api/v1/stats", headers=bearer(first)).status_code == 200
    assert client.get("/pool-api/v1/stats", headers=bearer(first)).status_code == 429
    assert client.get("/pool-api/v1/stats", headers=bearer(second)).status_code == 200


def test_authorized_access_is_audited_but_anonymous_is_not(client):
    raw = create_key(client)["api_key"]
    client.get("/pool-api/v1/stats", headers=bearer(raw))
    client.get("/pool-api/v1/stats")  # 匿名 401：不写审计，否则公网扫描能无限灌行
    with Session(client.app.state.engine) as db:
        events = db.scalars(
            select(DeliveryAuditEvent).where(DeliveryAuditEvent.event_type == pool.EVENT_API_ACCESS)
        ).all()
    assert len(events) == 1
    assert events[0].details["endpoint"] == "/pool-api/v1/stats"
    assert events[0].details["status"] == "ok"


# ------------------------------------------------------------------ 入参校验（错误码照 OEP）


def test_length_validators_use_oep_codes():
    cases = [
        (pool.validate_caller_id, "   ", "caller_id_empty"),
        (pool.validate_caller_id, "x" * 65, "caller_id_too_long"),
        (pool.validate_task_id, "", "task_id_empty"),
        (pool.validate_task_id, "x" * 129, "task_id_too_long"),
        (pool.validate_project_key, "x" * 129, "project_key_too_long"),
        (pool.validate_email_domain, "x" * 129, "email_domain_too_long"),
        (pool.validate_provider, "gmail", "invalid_provider"),
        (pool.validate_result, "failed", "invalid_result"),
    ]
    for validator, value, expected in cases:
        with pytest.raises(pool.PoolError) as excinfo:
            validator(value)
        assert excinfo.value.error_code == expected, (validator.__name__, value)
        assert excinfo.value.http_status == 400


def test_optional_validators_accept_blank_as_absent():
    assert pool.validate_project_key("  ") == ""
    assert pool.validate_email_domain("  ") == ""
    assert pool.validate_email_domain("PLUS.Test ") == "plus.test"
    assert pool.validate_provider("") is None
    assert pool.validate_result("verification_timeout") == "verification_timeout"


def test_text_and_lease_bounds():
    with pytest.raises(pool.PoolError) as excinfo:
        pool.validate_text("x" * 513, field="detail", max_len=pool.DETAIL_MAX_LEN)
    assert excinfo.value.error_code == "detail_too_long"
    with pytest.raises(pool.PoolError) as excinfo:
        pool.validate_text("x" * 257, field="reason", max_len=pool.REASON_MAX_LEN)
    assert excinfo.value.error_code == "reason_too_long"

    settings = Settings(pool_default_lease_seconds=600, pool_max_lease_seconds=3600)
    assert pool.validate_lease_seconds(None, settings=settings) == 600
    assert pool.validate_lease_seconds(900, settings=settings) == 900
    with pytest.raises(pool.PoolError) as excinfo:
        pool.validate_lease_seconds(0, settings=settings)
    assert excinfo.value.error_code == "lease_seconds_invalid"
    with pytest.raises(pool.PoolError) as excinfo:
        pool.validate_lease_seconds(3601, settings=settings)
    assert excinfo.value.error_code == "lease_seconds_too_long"


def test_envelope_uppercases_codes():
    assert pool.public_code("no_available_account") == "NO_AVAILABLE_ACCOUNT"
    assert pool.public_code(None) == "INTERNAL_ERROR"
    assert pool.ok({"a": 1}) == {"success": True, "code": "OK", "message": "success", "data": {"a": 1}}
    assert pool.fail("bad", "坏了") == {"success": False, "code": "BAD", "message": "坏了", "data": None}


# ------------------------------------------------------------------ 项目 / 域名范围


def test_key_project_isolates_projects():
    bound = PoolApiKey(name="k", project="proj-a")
    assert pool.effective_project(bound, "proj-a") == "proj-a"
    assert pool.effective_project(bound, "") == "proj-a"
    with pytest.raises(pool.PoolError) as excinfo:
        pool.effective_project(bound, "proj-b")
    assert excinfo.value.error_code == "project_mismatch"
    assert excinfo.value.http_status == 403
    assert pool.effective_project(PoolApiKey(name="k2", project=""), "proj-b") == "proj-b"


def test_domain_allowlist_blocks_other_domains():
    key = PoolApiKey(name="k", allowed_domains=["plus.test"])
    pool.ensure_domain_allowed(key, "plus.test")
    pool.ensure_domain_allowed(key, "")
    with pytest.raises(pool.PoolError) as excinfo:
        pool.ensure_domain_allowed(key, "other.test")
    assert excinfo.value.error_code == "domain_not_allowed"
    assert excinfo.value.http_status == 403
    pool.ensure_domain_allowed(PoolApiKey(name="k2"), "anything.test")


# ------------------------------------------------------------------ 可用性判据与统计


def test_domain_filter_anchors_at_the_end_of_address(client):
    seed_mailbox(client, address="a@plus.test")
    seed_mailbox(client, address="b@notplus.test")
    seed_mailbox(client, address="c@plus.test", source_label="cloudflare_temp_email")
    with Session(client.app.state.engine) as db:
        rows = db.scalars(
            select(InventoryMailbox.address).where(*pool.domain_conditions("plus.test"))
        ).all()
    # b@notplus.test 不许被 LIKE '%plus.test' 命中。
    assert sorted(rows) == ["a@plus.test", "c@plus.test"]


def test_provider_mapping_normalizes_labels_and_never_matches_oep_only_sources(client):
    seed_mailbox(client, address="a@plus.test", source_label="outlook_plus")
    seed_mailbox(client, address="b@hme.test", source_label="icloud-hme")
    seed_mailbox(client, address="c@cy.test", source_label="CYMail")
    with Session(client.app.state.engine) as db:
        outlook = db.scalars(select(InventoryMailbox.address).where(*pool.source_conditions("outlook"))).all()
        imap = db.scalars(
            select(InventoryMailbox.address).where(*pool.source_conditions("imap")).order_by(InventoryMailbox.address)
        ).all()
        custom = db.scalars(select(InventoryMailbox.address).where(*pool.source_conditions("custom"))).all()
        unfiltered = db.scalars(select(InventoryMailbox.address).where(*pool.source_conditions(None))).all()
    assert outlook == ["a@plus.test"]  # 只有 outlook_plus 映射到 OEP 的 outlook
    assert imap == ["b@hme.test", "c@cy.test"]  # icloud-hme / CYMail 归一化后都命中 imap
    assert custom == []  # OEP 自带来源，平台没有对应物：合法但不命中
    assert len(unfiltered) == 3


def test_stats_counts_by_pool_status_and_flags_unusable_available(client):
    seed_mailbox(client, address="ok@plus.test")
    seed_mailbox(client, address="missing@plus.test", status="missing")
    seed_mailbox(client, address="manual@plus.test", source_label="handwritten")
    seed_mailbox(client, address="expired@plus.test", expires_at=utc_now() - timedelta(seconds=1))
    seed_mailbox(client, address="used@plus.test", pool_status=pool.POOL_USED)
    raw = create_key(client)["api_key"]

    data = client.get("/pool-api/v1/stats", headers=bearer(raw)).json()["data"]
    assert data["pool_counts"][pool.POOL_AVAILABLE] == 4  # 五行里只有 used 那行不是 available
    assert data["pool_counts"][pool.POOL_USED] == 1
    assert data["pool_counts"][pool.POOL_CLAIMED] == 0
    # 诊断：三行虽然标着 available，但库存侧已经发不出去（上游消失/来源不可读/已过期）。
    assert data["unusable_available"] == 3


def test_usage_gate_only_excludes_same_caller_and_project(client):
    mailbox_id = seed_mailbox(client, address="g@plus.test")
    with Session(client.app.state.engine) as db:
        db.add(
            PoolClaim(
                mailbox_id=mailbox_id,
                caller_id="caller-1",
                task_id="task-1",
                project="proj-a",
                claim_token="done-token",
                status=pool.CLAIM_COMPLETED,
                claimed_at=utc_now(),
                lease_expires_at=utc_now(),
                result="success",
            )
        )
        db.commit()
        assert db.scalars(
            select(InventoryMailbox.id).where(*pool.usage_conditions(caller_id="caller-1", project="proj-a"))
        ).all() == []
        assert db.scalars(
            select(InventoryMailbox.id).where(*pool.usage_conditions(caller_id="caller-1", project="proj-b"))
        ).all() == [mailbox_id]
        assert db.scalars(
            select(InventoryMailbox.id).where(*pool.usage_conditions(caller_id="caller-2", project="proj-a"))
        ).all() == [mailbox_id]
        # 没有 caller 或没有项目时不做排除（OEP 的条件是 caller+project 同时具备）。
        assert db.scalars(select(InventoryMailbox.id).where(*pool.usage_conditions(caller_id="", project=""))).all() == [
            mailbox_id
        ]


# ------------------------------------------------------------------ 运维接管


def test_ops_can_freeze_retire_and_reset_mailbox(client):
    mailbox_id = seed_mailbox(client, address="f@plus.test")
    url = f"/delivery-api/v1/pool/mailboxes/{mailbox_id}"

    frozen = client.patch(url, headers=ADMIN, json={"pool_status": "frozen", "reason": "送信被封"})
    assert frozen.status_code == 200
    assert frozen.json() == {"id": mailbox_id, "pool_status": "frozen", "released_claims": None}

    assert client.patch(url, headers=ADMIN, json={"pool_status": "retired"}).json()["pool_status"] == "retired"
    assert client.patch(url, headers=ADMIN, json={"pool_status": "available"}).json()["pool_status"] == "available"

    with Session(client.app.state.engine) as db:
        events = db.scalars(
            select(DeliveryAuditEvent).where(DeliveryAuditEvent.event_type == pool.EVENT_MAILBOX_STATUS)
        ).all()
    assert [event.details["pool_status"] for event in events] == ["frozen", "retired", "available"]


def test_ops_reset_releases_active_lease(client):
    mailbox_id = seed_mailbox(client, address="h@plus.test")
    hold_claim(client, mailbox_id)

    response = client.patch(
        f"/delivery-api/v1/pool/mailboxes/{mailbox_id}",
        headers=ADMIN,
        json={"pool_status": "available", "reason": "手工回收"},
    )
    assert response.json() == {"id": mailbox_id, "pool_status": "available", "released_claims": 1}

    with Session(client.app.state.engine) as db:
        claim = db.scalar(select(PoolClaim).where(PoolClaim.mailbox_id == mailbox_id))
        assert claim is not None
        assert claim.status == pool.CLAIM_RELEASED
        assert claim.result_detail == "手工回收"
        row = db.get(InventoryMailbox, mailbox_id)
        assert row is not None
        assert row.pool_lease_expires_at is None
        assert row.pool_cooldown_until is None


def test_ops_cannot_set_status_that_belongs_to_the_lease_flow(client):
    mailbox_id = seed_mailbox(client, address="i@plus.test")
    response = client.patch(
        f"/delivery-api/v1/pool/mailboxes/{mailbox_id}", headers=ADMIN, json={"pool_status": "claimed"}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "POOL_MAILBOX_STATUS_UNSUPPORTED"
    assert (
        client.patch(
            "/delivery-api/v1/pool/mailboxes/does-not-exist", headers=ADMIN, json={"pool_status": "frozen"}
        ).status_code
        == 404
    )


def test_pickup_allocation_refuses_mailbox_leased_by_pool(client):
    mailbox_id = seed_mailbox(client, address="j@plus.test")
    hold_claim(client, mailbox_id)
    response = client.post(
        "/delivery-api/v1/inventory/allocations",
        headers=ADMIN,
        json={"mailbox_id": mailbox_id, "external_ref": "order-pool", "ttl_seconds": 600},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DELIVERY_MAILBOX_POOL_CLAIMED"
