"""E3：回传结果与统计（幂等、参数一致性、与账实一致）。

回传是池里唯一会「消耗掉」邮箱的动作，所以这里逐条锁死：
五种结果各自落到哪个池状态、重复回传只生效一次、运营手工状态不被流程覆盖、
stats 的数字与库里的实际记录一致。
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_pool_claim import ADMIN, bearer, claim, create_key, seed_mailbox

from delivery_api import pool
from delivery_api.models import InventoryMailbox, PoolClaim, utc_now


def complete(client, raw: str, data: dict, **body) -> dict:
    response = client.post(
        "/pool-api/v1/claim-complete",
        headers=bearer(raw),
        json={
            "account_id": data["account_id"],
            "claim_token": data["claim_token"],
            "caller_id": "caller-1",
            "task_id": "task-1",
            **body,
        },
    )
    return {"status_code": response.status_code, **response.json()}


def mailbox_status(client, address: str) -> str:
    with Session(client.app.state.engine) as db:
        row = db.scalar(select(InventoryMailbox).where(InventoryMailbox.address == address))
        assert row is not None
        return row.pool_status


def test_success_retires_the_mailbox_for_everyone(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="ok@plus.test")
    data = claim(client, raw)["data"]

    body = complete(client, raw, data, result="success", detail="注册成功")
    assert body["status_code"] == 200
    assert body["data"] == {"account_id": data["account_id"], "pool_status": "used"}
    assert mailbox_status(client, "ok@plus.test") == pool.POOL_USED

    with Session(client.app.state.engine) as db:
        stored = db.scalar(select(PoolClaim))
        assert stored is not None
        assert stored.status == pool.CLAIM_COMPLETED
        assert stored.result == "success" and stored.result_detail == "注册成功"
        assert stored.completed_at is not None

    # 成功过的邮箱不再发给任何人（OEP：success → used，全局退出池）。
    assert claim(client, raw)["code"] == "NO_AVAILABLE_ACCOUNT"


def test_verification_timeout_parks_in_cooldown(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="slow@plus.test")
    data = claim(client, raw)["data"]

    body = complete(client, raw, data, result="verification_timeout")
    assert body["data"]["pool_status"] == "cooldown"
    settings = client.app.state.settings

    with Session(client.app.state.engine) as db:
        row = db.scalar(select(InventoryMailbox).where(InventoryMailbox.address == "slow@plus.test"))
        assert row is not None
        assert row.pool_status == pool.POOL_COOLDOWN
        assert row.pool_cooldown_until is not None
        assert row.pool_lease_expires_at is None

        # 冷却期内不再发：领取会以 no_available_account 告终（HTTP 200 语义）。
        within = utc_now() + timedelta(seconds=1)
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
                now=within,
            )
        assert excinfo.value.error_code == "no_available_account"
        # ……到期后回来。
        later = utc_now() + timedelta(seconds=settings.pool_cooldown_seconds + 1)
        assert pool.release_finished_cooldowns(db, settings, later) == 1
        db.commit()
    assert claim(client, raw)["data"]["email"] == "slow@plus.test"


def test_network_error_returns_the_mailbox_immediately(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="net@plus.test")
    data = claim(client, raw)["data"]

    body = complete(client, raw, data, result="network_error")
    assert body["data"]["pool_status"] == "available"
    assert claim(client, raw)["data"]["email"] == "net@plus.test"


def test_blocked_and_invalid_credentials_take_the_mailbox_out_of_the_pool(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="blocked@plus.test")
    seed_mailbox(client, address="dead@plus.test")

    first = claim(client, raw)["data"]
    assert complete(client, raw, first, result="provider_blocked")["data"]["pool_status"] == "frozen"
    second = claim(client, raw)["data"]
    assert complete(client, raw, second, result="credential_invalid")["data"]["pool_status"] == "retired"

    assert {mailbox_status(client, "blocked@plus.test"), mailbox_status(client, "dead@plus.test")} == {
        pool.POOL_FROZEN,
        pool.POOL_RETIRED,
    }
    assert claim(client, raw)["code"] == "NO_AVAILABLE_ACCOUNT"


def test_second_complete_on_the_same_claim_does_nothing(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="dup@plus.test")
    data = claim(client, raw)["data"]

    assert complete(client, raw, data, result="success")["status_code"] == 200
    again = complete(client, raw, data, result="network_error")  # 想改口：不生效
    assert again["status_code"] == 409
    assert again["code"] == "NOT_CLAIMED"
    assert mailbox_status(client, "dup@plus.test") == pool.POOL_USED

    with Session(client.app.state.engine) as db:
        stored = db.scalar(select(PoolClaim))
        assert stored is not None
        assert stored.result == "success"  # 第一次的结果没被覆盖


def test_complete_validates_result_and_ownership(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="bad@plus.test")
    data = claim(client, raw)["data"]

    missing = complete(client, raw, data)
    assert missing["status_code"] == 400 and missing["code"] == "INVALID_RESULT"
    unknown = complete(client, raw, data, result="failed")
    assert unknown["status_code"] == 400 and unknown["code"] == "INVALID_RESULT"
    long_detail = complete(client, raw, data, result="success", detail="x" * 513)
    assert long_detail["status_code"] == 400 and long_detail["code"] == "DETAIL_TOO_LONG"
    wrong_token = complete(client, raw, {**data, "claim_token": "nope"}, result="success")
    assert wrong_token["status_code"] == 403 and wrong_token["code"] == "TOKEN_MISMATCH"

    # 参数错一次都不许改动邮箱状态与租约。
    assert mailbox_status(client, "bad@plus.test") == pool.POOL_CLAIMED


def test_ops_decision_is_not_overwritten_by_completion(client):
    raw = create_key(client)["api_key"]
    mailbox_id = seed_mailbox(client, address="ops@plus.test")
    data = claim(client, raw)["data"]

    # 运营在被租用期间把这个邮箱冻结（比如确认它被上游封了）。
    client.patch(
        f"/delivery-api/v1/pool/mailboxes/{mailbox_id}",
        headers=ADMIN,
        json={"pool_status": "frozen", "reason": "上游封禁"},
    )
    body = complete(client, raw, data, result="success")
    # 回传照常记账，但不把运营的冻结覆盖成 used，且如实回报实际状态。
    assert body["data"]["pool_status"] == "frozen"
    assert mailbox_status(client, "ops@plus.test") == pool.POOL_FROZEN
    with Session(client.app.state.engine) as db:
        stored = db.scalar(select(PoolClaim))
        assert stored is not None and stored.status == pool.CLAIM_COMPLETED and stored.result == "success"


def test_usage_gate_blocks_same_caller_project_after_ops_reset(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="m1@plus.test")
    seed_mailbox(client, address="m2@plus.test")

    taken = claim(client, raw)["data"]  # 随机给哪个都行，下面按它推另一个
    complete(client, raw, taken, result="success")
    succeeded, spare = taken["email"], ""
    spare = "m2@plus.test" if succeeded == "m1@plus.test" else "m1@plus.test"
    with Session(client.app.state.engine) as db:
        succeeded_id = db.scalar(select(InventoryMailbox.id).where(InventoryMailbox.address == succeeded))
    assert isinstance(succeeded_id, str)

    # 运营把它放回池里（模拟「这个号还能再用」），但同一个 caller + 项目不该再拿到它。
    client.patch(f"/delivery-api/v1/pool/mailboxes/{succeeded_id}", headers=ADMIN, json={"pool_status": "available"})
    again = claim(client, raw)["data"]
    assert again["email"] == spare

    # 换一个 caller（另一个项目）则可以拿到它：OEP v22 的语义是「同 caller+项目只防 success」。
    other = claim(client, raw, caller_id="caller-2", task_id="task-2", project_key="proj-b")["data"]
    assert other["email"] == succeeded


def test_stats_match_the_actual_records(client):
    raw = create_key(client)["api_key"]
    seed_mailbox(client, address="a@plus.test")
    seed_mailbox(client, address="b@plus.test")
    seed_mailbox(client, address="c@plus.test")
    seed_mailbox(client, address="d@plus.test")

    done = claim(client, raw)["data"]
    complete(client, raw, done, result="success")
    timeouts = claim(client, raw)["data"]
    complete(client, raw, timeouts, result="verification_timeout")
    released = claim(client, raw)["data"]
    client.post(
        "/pool-api/v1/claim-release",
        headers=bearer(raw),
        json={
            "account_id": released["account_id"],
            "claim_token": released["claim_token"],
            "caller_id": "caller-1",
            "task_id": "task-1",
        },
    )
    claim(client, raw)  # 还在被租用的那个

    reported = client.get("/pool-api/v1/stats", headers=bearer(raw)).json()["data"]["pool_counts"]
    with Session(client.app.state.engine) as db:
        actual = dict.fromkeys(pool.POOL_STATUSES, 0)
        for status, total in db.execute(
            select(InventoryMailbox.pool_status, func.count()).group_by(InventoryMailbox.pool_status)
        ).all():
            if status in actual:
                actual[status] = total
        # 账实一致：stats 不是另算一套，它读的就是这张表。
        assert reported == actual
        # 不变式：池状态与领取记录不许互相矛盾。
        held = db.scalar(
            select(func.count()).select_from(PoolClaim).where(PoolClaim.status == pool.CLAIM_CLAIMED)
        )
        assert held == reported[pool.POOL_CLAIMED] == 1
    assert reported[pool.POOL_USED] == 1
    assert reported[pool.POOL_COOLDOWN] == 1
    assert reported[pool.POOL_AVAILABLE] == 1
    assert sum(reported.values()) == 4


def test_claim_complete_requires_a_pool_key(client):
    seed_mailbox(client, address="anon@plus.test")
    create_key(client)
    response = client.post("/pool-api/v1/claim-complete", json={"account_id": 1, "result": "success"})
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert mailbox_status(client, "anon@plus.test") == pool.POOL_AVAILABLE
