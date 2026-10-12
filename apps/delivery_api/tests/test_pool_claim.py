"""E2：原子领取、租约回收、释放。

并发那条是核心：20 个线程争同一个邮箱只能有 1 个赢。它用**文件库**跑——
测试夹具的内存库是 StaticPool（全线程共用一条连接），在那里测不出真并发，
CAS 写得不对也会「绿」。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from delivery_api import pool
from delivery_api.app import create_app
from delivery_api.models import Base, InventoryMailbox, PoolClaim, utc_now
from delivery_api.settings import Settings

OPERATOR = "operator-test-token"
ADMIN = {"Authorization": f"Bearer {OPERATOR}"}
CLAIM_BODY = {"caller_id": "caller-1", "task_id": "task-1", "project_key": "proj-a"}


def create_key(client, **overrides) -> dict:
    response = client.post("/delivery-api/v1/pool/keys", headers=ADMIN, json={"name": "注册机", **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def bearer(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def seed_mailbox(client, *, address: str = "a@plus.test", source_label: str = "outlook_plus", **fields) -> str:
    created = client.post(
        "/delivery-api/v1/inventory/mailboxes",
        headers=ADMIN,
        json={"address": address, "source_label": source_label, "messages": []},
    )
    assert created.status_code == 201, created.text
    mailbox_id = created.json()["id"]
    if fields:
        with Session(client.app.state.engine) as db:
            row = db.get(InventoryMailbox, mailbox_id)
            assert row is not None
            for name, value in fields.items():
                setattr(row, name, value)
            db.commit()
    return mailbox_id


def claim(client, raw: str, **body) -> dict:
    response = client.post("/pool-api/v1/claim-random", headers=bearer(raw), json={**CLAIM_BODY, **body})
    assert response.status_code in (200, 400, 403, 429), response.text
    return response.json()


# ------------------------------------------------------------------ 领取


def test_claim_random_returns_oep_fields_and_leases_mailbox(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="c@plus.test")

    body = claim(client, raw)
    assert body["success"] is True and body["code"] == "OK"
    data = body["data"]
    assert set(data) == {"account_id", "email", "email_domain", "claim_token", "claimed_at", "lease_expires_at"}
    assert isinstance(data["account_id"], int)  # 契约要求整数，不是 UUID
    assert data["email"] == "c@plus.test"
    assert data["email_domain"] == "plus.test"
    assert data["claim_token"]
    assert data["claimed_at"].endswith("Z") and data["lease_expires_at"].endswith("Z")

    with Session(client.app.state.engine) as db:
        mailbox = db.scalar(select(InventoryMailbox).where(InventoryMailbox.address == "c@plus.test"))
        assert mailbox is not None
        assert mailbox.pool_status == pool.POOL_CLAIMED
        assert mailbox.pool_lease_expires_at is not None
        stored = db.scalar(select(PoolClaim))
        assert stored is not None
        assert stored.id == data["account_id"]
        assert stored.status == pool.CLAIM_CLAIMED
        assert stored.caller_id == "caller-1" and stored.task_id == "task-1" and stored.project == "proj-a"


def test_empty_pool_is_http_200_with_success_false(client):
    raw = create_key(client)["api_key"]
    response = client.post("/pool-api/v1/claim-random", headers=bearer(raw), json=CLAIM_BODY)
    # 这条是注册机「没有号了」的分支依据：别改成 404/409。
    assert response.status_code == 200
    assert response.json() == {
        "success": False,
        "code": "NO_AVAILABLE_ACCOUNT",
        "message": "池中没有符合条件的可用邮箱",
        "data": None,
    }


def test_each_claim_takes_a_different_mailbox(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="a@plus.test")
    seed_mailbox(client, address="b@plus.test")

    first = claim(client, raw)["data"]
    second = claim(client, raw)["data"]
    assert {first["email"], second["email"]} == {"a@plus.test", "b@plus.test"}
    assert first["account_id"] != second["account_id"]
    assert first["claim_token"] != second["claim_token"]
    assert claim(client, raw)["code"] == "NO_AVAILABLE_ACCOUNT"


def test_claim_domain_requires_and_honours_domain(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="a@plus.test")
    seed_mailbox(client, address="b@other.test")

    missing = client.post("/pool-api/v1/claim-domain", headers=bearer(raw), json=CLAIM_BODY)
    assert missing.status_code == 400
    assert missing.json()["code"] == "EMAIL_DOMAIN_REQUIRED"

    hit = client.post(
        "/pool-api/v1/claim-domain", headers=bearer(raw), json={**CLAIM_BODY, "email_domain": "OTHER.test"}
    )
    assert hit.json()["data"]["email"] == "b@other.test"  # 域名大小写不敏感

    again = client.post(
        "/pool-api/v1/claim-domain", headers=bearer(raw), json={**CLAIM_BODY, "email_domain": "other.test"}
    )
    assert again.json()["code"] == "NO_AVAILABLE_ACCOUNT"


def test_provider_filter_maps_oep_vocabulary(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="o@plus.test", source_label="outlook_plus")
    seed_mailbox(client, address="h@hme.test", source_label="icloud-hme")

    imap = claim(client, raw, provider="imap")["data"]
    assert imap["email"] == "h@hme.test"  # icloud-hme 归一化后命中 imap 映射
    assert claim(client, raw, provider="imap")["code"] == "NO_AVAILABLE_ACCOUNT"

    # OEP 自带来源（custom/gptmail）平台没有对应物：取值合法、永不命中。
    assert claim(client, raw, provider="custom")["code"] == "NO_AVAILABLE_ACCOUNT"
    # 词表外的取值才是参数错。
    bad = client.post("/pool-api/v1/claim-random", headers=bearer(raw), json={**CLAIM_BODY, "provider": "gmail"})
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_PROVIDER"


def test_key_domain_allowlist_limits_what_can_be_claimed(client):
    raw = create_key(client, allowed_domains=["plus.test"])["api_key"]
    seed_mailbox(client, address="a@plus.test")
    seed_mailbox(client, address="b@other.test")

    claimed = claim(client, raw)["data"]
    assert claimed["email"] == "a@plus.test"  # 白名单外的域名随机也挑不到

    blocked = client.post(
        "/pool-api/v1/claim-domain", headers=bearer(raw), json={**CLAIM_BODY, "email_domain": "other.test"}
    )
    assert blocked.status_code == 403 and blocked.json()["code"] == "DOMAIN_NOT_ALLOWED"
    assert claim(client, raw)["code"] == "NO_AVAILABLE_ACCOUNT"


def test_key_bound_project_is_enforced(client):
    raw = create_key(client, project="proj-a")["api_key"]
    seed_mailbox(client, address="a@plus.test")

    mine = client.post("/pool-api/v1/claim-random", headers=bearer(raw), json=CLAIM_BODY)
    assert mine.json()["success"] is True
    other = client.post(
        "/pool-api/v1/claim-random", headers=bearer(raw), json={**CLAIM_BODY, "project_key": "proj-b"}
    )
    assert other.status_code == 403 and other.json()["code"] == "PROJECT_MISMATCH"


def test_unusable_inventory_is_never_handed_out(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="missing@plus.test", status="missing")
    seed_mailbox(client, address="manual@plus.test", source_label="handwritten")
    seed_mailbox(client, address="expired@plus.test", expires_at=utc_now() - timedelta(seconds=1))
    assert claim(client, raw)["code"] == "NO_AVAILABLE_ACCOUNT"


def test_concurrent_claims_on_one_mailbox_only_one_wins(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'pool.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    app = create_app(
        Settings(database_url="sqlite+pysqlite://", operator_token=OPERATOR, encryption_key="k"), engine=engine
    )
    with TestClient(app) as client:
        raw = create_key(client)["api_key"]
        seed_mailbox(client, address="only@plus.test")

        def attempt(_: int):
            return client.post("/pool-api/v1/claim-random", headers=bearer(raw), json=CLAIM_BODY)

        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(executor.map(attempt, range(20)))

        bodies = [response.json() for response in responses]
        winners = [body for body in bodies if body["success"]]
        assert len(winners) == 1, [body.get("code") for body in bodies]
        # 抢输的也是「池里没有可用邮箱」（200），不是 500、不是数据库锁错误。
        assert {response.status_code for response in responses} == {200}
        assert {body["code"] for body in bodies if not body["success"]} == {"NO_AVAILABLE_ACCOUNT"}

        with Session(engine) as db:
            assert len(db.scalars(select(PoolClaim)).all()) == 1
            assert db.scalar(select(InventoryMailbox.pool_status)) == pool.POOL_CLAIMED


# ------------------------------------------------------------------ 释放


def test_release_returns_mailbox_to_the_pool(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="r@plus.test")
    data = claim(client, raw)["data"]
    release_body = {
        "account_id": data["account_id"],
        "claim_token": data["claim_token"],
        "caller_id": "caller-1",
        "task_id": "task-1",
    }

    released = client.post("/pool-api/v1/claim-release", headers=bearer(raw), json=release_body)
    assert released.status_code == 200
    assert released.json() == {
        "success": True,
        "code": "OK",
        "message": "success",
        "data": {"account_id": data["account_id"], "pool_status": "available"},
    }

    again = client.post("/pool-api/v1/claim-release", headers=bearer(raw), json=release_body)
    assert again.status_code == 409 and again.json()["code"] == "NOT_CLAIMED"

    # 释放不计成功、不进冷却：同一个邮箱可以再被领走。
    reclaimed = claim(client, raw)["data"]
    assert reclaimed["email"] == "r@plus.test"
    assert reclaimed["account_id"] != data["account_id"]


def test_release_validates_ownership_and_keeps_the_lease_on_failure(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="s@plus.test")
    data = claim(client, raw)["data"]
    base = {
        "account_id": data["account_id"],
        "claim_token": data["claim_token"],
        "caller_id": "caller-1",
        "task_id": "task-1",
    }

    wrong_token = client.post("/pool-api/v1/claim-release", headers=bearer(raw), json={**base, "claim_token": "x"})
    assert wrong_token.status_code == 403 and wrong_token.json()["code"] == "TOKEN_MISMATCH"

    wrong_caller = client.post("/pool-api/v1/claim-release", headers=bearer(raw), json={**base, "caller_id": "other"})
    assert wrong_caller.status_code == 403 and wrong_caller.json()["code"] == "CALLER_MISMATCH"

    wrong_task = client.post("/pool-api/v1/claim-release", headers=bearer(raw), json={**base, "task_id": "other"})
    assert wrong_task.status_code == 403 and wrong_task.json()["code"] == "CALLER_MISMATCH"

    empty_token = client.post("/pool-api/v1/claim-release", headers=bearer(raw), json={**base, "claim_token": ""})
    assert empty_token.status_code == 400 and empty_token.json()["code"] == "CLAIM_TOKEN_EMPTY"

    unknown = client.post("/pool-api/v1/claim-release", headers=bearer(raw), json={**base, "account_id": 999999})
    assert unknown.status_code == 400 and unknown.json()["code"] == "ACCOUNT_NOT_FOUND"

    missing = client.post(
        "/pool-api/v1/claim-release",
        headers=bearer(raw),
        json={key: value for key, value in base.items() if key != "account_id"},
    )
    assert missing.status_code == 400 and missing.json()["code"] == "ACCOUNT_ID_MISSING"

    invalid = client.post("/pool-api/v1/claim-release", headers=bearer(raw), json={**base, "account_id": "abc"})
    assert invalid.status_code == 400 and invalid.json()["code"] == "ACCOUNT_ID_INVALID"

    # 一串失败的释放不能把租约弄没：邮箱仍属于那个领取。
    with Session(client.app.state.engine) as db:
        mailbox = db.scalar(select(InventoryMailbox).where(InventoryMailbox.address == "s@plus.test"))
        assert mailbox is not None and mailbox.pool_status == pool.POOL_CLAIMED
        stored = db.scalar(select(PoolClaim))
        assert stored is not None and stored.status == pool.CLAIM_CLAIMED


# ------------------------------------------------------------------ 租约与冷却（按时间推进，不睡等）


def test_lease_expiry_parks_mailbox_in_cooldown_then_releases_it(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="l@plus.test")
    claim(client, raw)
    settings = client.app.state.settings
    later = utc_now() + timedelta(seconds=settings.pool_default_lease_seconds + 1)

    with Session(client.app.state.engine) as db:
        assert pool.expire_stale_claims(db, settings, later) == 1
        db.commit()
        mailbox = db.scalar(select(InventoryMailbox).where(InventoryMailbox.address == "l@plus.test"))
        assert mailbox is not None
        assert mailbox.pool_status == pool.POOL_COOLDOWN
        assert mailbox.pool_cooldown_until is not None and mailbox.pool_lease_expires_at is None
        stored = db.scalar(select(PoolClaim))
        assert stored is not None and stored.status == pool.CLAIM_EXPIRED and stored.completed_at is not None

        # 冷却期内不再发放。
        with pytest.raises(pool.PoolError) as excinfo:
            pool.claim_mailbox(
                db,
                settings,
                caller_id="caller-1",
                task_id="task-1",
                provider=None,
                project="proj-a",
                email_domain="",
                lease_seconds=600,
                now=later,
            )
        assert excinfo.value.error_code == "no_available_account"
        assert excinfo.value.http_status == 200

        # 冷却到期自动归位，且能再领到同一个邮箱。
        after_cooldown = later + timedelta(seconds=settings.pool_cooldown_seconds + 1)
        assert pool.release_finished_cooldowns(db, settings, after_cooldown) == 1
        db.commit()
        reclaimed = pool.claim_mailbox(
            db,
            settings,
            caller_id="caller-1",
            task_id="task-1",
            provider=None,
            project="proj-a",
            email_domain="",
            lease_seconds=600,
            now=after_cooldown,
        )
        assert reclaimed["email"] == "l@plus.test"


def test_expired_lease_cannot_be_released_afterwards(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="x@plus.test")
    data = claim(client, raw)["data"]
    settings = client.app.state.settings
    later = utc_now() + timedelta(seconds=settings.pool_default_lease_seconds + 1)

    with Session(client.app.state.engine) as db:
        pool.expire_stale_claims(db, settings, later)
        db.commit()
        with pytest.raises(pool.PoolError) as excinfo:
            pool.release_claim(
                db,
                settings,
                account_id=data["account_id"],
                claim_token=data["claim_token"],
                caller_id="caller-1",
                task_id="task-1",
                now=later,
            )
        assert excinfo.value.error_code == "not_claimed"
        assert excinfo.value.http_status == 409


# ------------------------------------------------------------------ 幂等（平台扩展字段）


def test_idempotency_key_replays_the_same_claim(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="i1@plus.test")
    seed_mailbox(client, address="i2@plus.test")
    body = {**CLAIM_BODY, "idempotency_key": "reg-42"}

    first = client.post("/pool-api/v1/claim-random", headers=bearer(raw), json=body).json()["data"]
    replay = client.post("/pool-api/v1/claim-random", headers=bearer(raw), json=body).json()["data"]
    assert replay == first  # 同一条领取，不再消耗邮箱

    with Session(client.app.state.engine) as db:
        held = db.scalars(select(InventoryMailbox).where(InventoryMailbox.pool_status == pool.POOL_CLAIMED)).all()
    assert len(held) == 1

    client.post(
        "/pool-api/v1/claim-release",
        headers=bearer(raw),
        json={
            "account_id": first["account_id"],
            "claim_token": first["claim_token"],
            "caller_id": "caller-1",
            "task_id": "task-1",
        },
    )
    # 领取已结束：同一个幂等键再来要，明确 409，而不是偷偷再发一个邮箱。
    conflict = client.post("/pool-api/v1/claim-random", headers=bearer(raw), json=body)
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_claim_without_caller_or_task_is_rejected(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="v@plus.test")
    no_caller = client.post("/pool-api/v1/claim-random", headers=bearer(raw), json={"task_id": "t"})
    assert no_caller.status_code == 400 and no_caller.json()["code"] == "CALLER_ID_EMPTY"
    no_task = client.post("/pool-api/v1/claim-random", headers=bearer(raw), json={"caller_id": "c"})
    assert no_task.status_code == 400 and no_task.json()["code"] == "TASK_ID_EMPTY"
    # 参数错不能把邮箱消耗掉。
    with Session(client.app.state.engine) as db:
        assert db.scalar(select(InventoryMailbox.pool_status)) == pool.POOL_AVAILABLE
