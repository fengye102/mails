"""定时创建调度测试（对照 Go `scheduler_test.go` 的取舍，但按本项目的「一任务一账号」模型）。

核心是把「什么时候该继续、什么时候该停」钉住：达到目标就停、总容量满就停、
被限速就退避、其他错误留痕后继续，以及界面填 0 间隔不能被真的当 0 用。
"""

import base64
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from icloud_api.alias_service import CreateOutcome, classify
from icloud_api.aliases import (
    AliasError,
    AliasLimitReached,
    AliasRateLimited,
    alias_retry_after,
    is_alias_limit_reached,
    is_alias_rate_limited,
    root_alias_error,
)
from icloud_api.models import ICloudAccount, ICloudSchedulerJob
from icloud_api.rate_limit import AliasCreateRateLimiter
from icloud_api.scheduler import (
    MAX_EVENTS,
    MIN_INTERVAL_SECONDS,
    SchedulerService,
    add_event,
    default_job_for_account,
    effective_interval,
    is_due,
    reached_target,
    run_round,
)
from icloud_api.settings import Settings

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _job(**overrides: object) -> ICloudSchedulerJob:
    job = ICloudSchedulerJob(
        account_id="acct-1",
        enabled=True,
        target_count=0,
        interval_seconds=180,
        created_count=0,
        last_error="",
        events=[],
    )
    for field, value in overrides.items():
        setattr(job, field, value)
    return job


def _events(job: ICloudSchedulerJob) -> list[dict]:
    """events 是可空 JSON 列，测试里统一成列表读。"""
    return list(job.events or [])


def _ok(email: str = "new@icloud.com", interface: str = "apple_account") -> CreateOutcome:
    return CreateOutcome(email=email, label="L", created_at="2026-10-12T12:00:00Z", interface=interface)


def _raiser(error: BaseException):
    async def _fn() -> CreateOutcome:
        raise error

    return _fn


# ---- 到点判断 ----


def test_disabled_job_is_never_due() -> None:
    assert is_due(_job(enabled=False), NOW) is False


def test_job_without_next_run_is_due() -> None:
    assert is_due(_job(), NOW) is True


def test_job_waits_until_next_run() -> None:
    assert is_due(_job(next_run_at=NOW + timedelta(minutes=1)), NOW) is False
    assert is_due(_job(next_run_at=NOW - timedelta(minutes=1)), NOW) is True


def test_naive_next_run_at_is_read_as_utc() -> None:
    """SQLite 取回的 datetime 可能没有时区，不能被当成「还没到」。"""
    naive_past = (NOW - timedelta(minutes=5)).replace(tzinfo=None)
    assert is_due(_job(next_run_at=naive_past), NOW) is True


def test_target_zero_means_unlimited() -> None:
    assert reached_target(_job(target_count=0, created_count=999)) is False
    assert reached_target(_job(target_count=3, created_count=3)) is True


def test_interval_has_a_floor() -> None:
    """界面填 0 或很小的间隔不能被真的按原值用，否则会把账号打爆。"""
    assert effective_interval(_job(interval_seconds=0)) == timedelta(seconds=MIN_INTERVAL_SECONDS)
    assert effective_interval(_job(interval_seconds=600)) == timedelta(seconds=600)


# ---- 单轮 ----


@pytest.mark.asyncio
async def test_disabled_job_does_not_create() -> None:
    job = _job(enabled=False)
    called = False

    async def create() -> CreateOutcome:
        nonlocal called
        called = True
        return _ok()

    result = await run_round(job, now=NOW, create_fn=create)
    assert result.status == "idle"
    assert called is False


@pytest.mark.asyncio
async def test_success_counts_and_schedules_next_round() -> None:
    job = _job(interval_seconds=180)
    limiter = AliasCreateRateLimiter()

    result = await run_round(job, now=NOW, create_fn=lambda: _async(_ok()), limiter=limiter)

    assert result.status == "created"
    assert result.email == "new@icloud.com"
    assert result.interface == "apple_account"
    assert job.created_count == 1
    assert job.last_run_at == NOW
    assert job.next_run_at == NOW + timedelta(seconds=180)
    assert job.enabled is True
    assert job.last_error == ""
    assert any("创建成功" in event["message"] for event in _events(job))


@pytest.mark.asyncio
async def test_reaching_target_auto_stops() -> None:
    job = _job(target_count=1)

    result = await run_round(job, now=NOW, create_fn=lambda: _async(_ok()))

    assert result.disable is True
    assert job.enabled is False
    assert job.created_count == 1
    assert any("自动停止" in event["message"] for event in _events(job))


@pytest.mark.asyncio
async def test_already_at_target_stops_without_creating() -> None:
    job = _job(target_count=2, created_count=2)
    called = False

    async def create() -> CreateOutcome:
        nonlocal called
        called = True
        return _ok()

    result = await run_round(job, now=NOW, create_fn=create)
    assert result.status == "done"
    assert result.disable is True
    assert called is False, "已达目标不应再调上游"


@pytest.mark.asyncio
async def test_limit_reached_stops_the_job() -> None:
    """总容量已满 → 自动停（不回退、不再轮次白耗）。"""
    job = _job()
    error = AliasLimitReached("总容量已满")

    result = await run_round(job, now=NOW, create_fn=_raiser(error))

    assert result.status == "limit_reached"
    assert job.enabled is False
    assert job.next_run_at is None
    assert "总容量已满" in job.last_error
    assert job.created_count == 0


@pytest.mark.asyncio
async def test_rate_limited_backs_off_for_suggested_duration() -> None:
    job = _job()
    limiter = AliasCreateRateLimiter()
    error = AliasRateLimited("限速", retry_after=timedelta(minutes=30))

    result = await run_round(job, now=NOW, create_fn=_raiser(error), limiter=limiter)

    assert result.status == "rate_limited"
    assert result.next_delay == timedelta(minutes=30)
    assert job.next_run_at == NOW + timedelta(minutes=30)
    assert job.enabled is True, "限速只是退避，不该关掉任务"
    # 限速应登记到共享限流器，避免手工/调度两条路径互相踩
    assert limiter.retry_after(job.account_id, NOW, timedelta(seconds=180)) > timedelta(0)


@pytest.mark.asyncio
async def test_rate_limited_without_retry_after_defaults_to_one_hour() -> None:
    job = _job()
    result = await run_round(job, now=NOW, create_fn=_raiser(AliasRateLimited("限速")))
    assert result.next_delay == timedelta(hours=1)


@pytest.mark.asyncio
async def test_unexpected_failure_keeps_job_running() -> None:
    job = _job(interval_seconds=180)
    error = RuntimeError("网络抖动")

    result = await run_round(job, now=NOW, create_fn=_raiser(error))

    assert result.status == "failed"
    assert job.enabled is True
    assert job.last_error == "网络抖动"
    assert job.next_run_at == NOW + timedelta(seconds=180)
    assert any("创建失败" in event["message"] for event in _events(job))


@pytest.mark.asyncio
async def test_retry_after_from_limiter_skips_round() -> None:
    """手工创建刚成功后，调度轮次应尊重同一个限流器而不打上游。"""
    job = _job(interval_seconds=180)
    limiter = AliasCreateRateLimiter()
    limiter.record_success(job.account_id, NOW)
    called = False

    async def create() -> CreateOutcome:
        nonlocal called
        called = True
        return _ok()

    result = await run_round(job, now=NOW + timedelta(seconds=10), create_fn=create, limiter=limiter)

    assert result.status == "not_due"
    assert called is False


# ---- 事件上限 ----


def test_events_are_capped_fifo() -> None:
    job = _job()
    for index in range(MAX_EVENTS + 20):
        add_event(job, NOW, f"事件 {index}")
    events = _events(job)
    assert len(events) == MAX_EVENTS
    assert events[-1]["message"] == f"事件 {MAX_EVENTS + 19}"
    assert events[0]["message"] == "事件 20"


# ---- 新建任务默认值 ----


def test_new_job_is_disabled_by_default() -> None:
    """定时创建必须由界面显式打开——默认绝不能是开着的。"""
    settings = Settings(alias_default_count=3, alias_min_interval_seconds=120)
    job = default_job_for_account("acct-9", settings)
    assert job.enabled is False
    assert job.target_count == 3
    assert job.interval_seconds == 120


# ---- SchedulerService 端到端（真库，假创建动作）----


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def data_key() -> bytes:
    return base64.b64decode(base64.b64encode(os.urandom(32)))


def _settings(data_key: bytes) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        operator_token="operator-test-token",
        data_key=base64.b64encode(data_key).decode("ascii"),
    )


@pytest.mark.asyncio
async def test_tick_creates_and_persists(session_factory, data_key: bytes) -> None:
    """tick 走真库：取到点任务 → 建一个别名 → 计数与时间落库。"""
    with session_factory() as db:
        account = ICloudAccount(name="主号", real_email="u@example.com", host="icloud.com")
        db.add(account)
        db.flush()
        job = default_job_for_account(account.id, _settings(data_key), target_count=0, interval_seconds=180)
        job.enabled = True  # 必须显式打开（默认关是刻意的）
        db.add(job)
        db.commit()
        account_id, job_id = account.id, job.id

    calls: list[str] = []

    async def fake_create(account, key, **kwargs):
        calls.append(account.id)
        return _ok(email="round1@icloud.com", interface="icloud_web")

    service = SchedulerService(
        session_factory, _settings(data_key), create_alias=fake_create, tick_seconds=1
    )

    results = await service.tick(NOW)

    assert [result.status for _, result in results] == ["created"]
    assert calls == [account_id]
    with session_factory() as db:
        saved = db.get(ICloudSchedulerJob, job_id)
        assert saved is not None
        assert saved.created_count == 1
        assert saved.last_run_at is not None
        assert saved.next_run_at is not None
        assert any("round1@icloud.com" in event["message"] for event in _events(saved))


@pytest.mark.asyncio
async def test_tick_ignores_disabled_jobs(session_factory, data_key: bytes) -> None:
    with session_factory() as db:
        account = ICloudAccount(name="主号", real_email="u@example.com", host="icloud.com")
        db.add(account)
        db.flush()
        db.add(default_job_for_account(account.id, _settings(data_key)))  # enabled=False
        db.commit()

    async def should_not_run(*args, **kwargs):
        raise AssertionError("禁用的任务不应触发创建")

    service = SchedulerService(session_factory, _settings(data_key), create_alias=should_not_run)
    assert await service.tick(NOW) == []


@pytest.mark.asyncio
async def test_tick_stops_job_when_account_deleted(session_factory, data_key: bytes) -> None:
    """账号先被删掉、任务还挂着（外键关掉的极端场景）→ 任务自动停，不报错。"""
    with session_factory() as db:
        account = ICloudAccount(name="主号", real_email="u@example.com", host="icloud.com")
        db.add(account)
        db.flush()
        job = default_job_for_account(account.id, _settings(data_key))
        job.enabled = True
        db.add(job)
        db.commit()
        job_id, account_id = job.id, account.id

    # 外键约束会拦住「删账号但留任务」，这里显式关掉外键来构造这个孤儿状态。
    with session_factory() as db:
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.execute(text("DELETE FROM icloud_accounts WHERE id = :id"), {"id": account_id})
        db.commit()

    service = SchedulerService(session_factory, _settings(data_key), create_alias=_async_ok)
    results = await service.tick(NOW)

    assert [result.status for _, result in results] == ["done"]
    with session_factory() as db:
        assert db.get(ICloudSchedulerJob, job_id).enabled is False


@pytest.mark.asyncio
async def test_wrapped_limit_reached_still_stops_job() -> None:
    """真缺陷回归：上游客户端会把原始错误包一层再抛（`raise ... from last_error`）。

    只认最外层的话，「容量满」会被当成普通失败 → 任务不停 → 一直对着已满账号
    重试到被 Apple 封禁。必须穿透包装层识别分类。
    """
    job = _job()
    root = AliasLimitReached("总容量已满")
    wrapped = AliasError("ALIAS_CREATE_FAILED", f"创建别名失败: {root}")
    wrapped.__cause__ = root

    result = await run_round(job, now=NOW, create_fn=_raiser(wrapped))

    assert result.status == "limit_reached"
    assert job.enabled is False, "包装层不能挡住「自动停」"
    assert job.next_run_at is None


@pytest.mark.asyncio
async def test_wrapped_rate_limited_uses_inner_retry_after() -> None:
    """包装层的 __cause__ 里才有真实退避时长；拿不到就会退化成「立刻重试」。"""
    job = _job()
    limiter = AliasCreateRateLimiter()
    root = AliasRateLimited("限速", retry_after=timedelta(minutes=45))
    wrapped = AliasError("ALIAS_CREATE_FAILED", f"创建别名失败: {root}")
    wrapped.__cause__ = root

    result = await run_round(job, now=NOW, create_fn=_raiser(wrapped), limiter=limiter)

    assert result.status == "rate_limited"
    assert result.next_delay == timedelta(minutes=45), "要用内层的 retry_after，不是默认 1 小时"
    assert job.next_run_at == NOW + timedelta(minutes=45)
    assert limiter.retry_after(job.account_id, NOW, timedelta(seconds=180)) > timedelta(0)


def test_root_alias_error_prefers_capacity_over_rate_limit() -> None:
    """链上同时有限速和容量时，容量优先——它决定任务必须停，而不是退避。"""
    rate = AliasRateLimited("限速", retry_after=timedelta(minutes=10))
    capacity = AliasLimitReached("满了")
    capacity.__context__ = rate
    assert root_alias_error(capacity) is capacity

    outer = AliasError("ALIAS_CREATE_FAILED", "包一层")
    outer.__cause__ = capacity
    assert isinstance(root_alias_error(outer), AliasLimitReached)
    assert is_alias_limit_reached(outer) is True
    assert is_alias_rate_limited(outer) is False


def test_alias_retry_after_walks_the_chain() -> None:
    root = AliasRateLimited("限速", retry_after=timedelta(seconds=90))
    wrapped = AliasError("ALIAS_CREATE_FAILED", "包一层")
    wrapped.__cause__ = root
    assert alias_retry_after(wrapped) == timedelta(seconds=90)
    assert alias_retry_after(AliasError("X", "无关")) == timedelta(0)


async def _async(value: CreateOutcome) -> CreateOutcome:
    return value


async def _async_ok(*args, **kwargs):
    return _ok()


def test_classify_wires_through_to_scheduler() -> None:
    """scheduler 抛出的错误码应能被路由层用的 classify 识别（同一套语义）。"""
    assert classify(AliasLimitReached("满")).code == "ALIAS_LIMIT_REACHED"
    assert classify(AliasRateLimited("限速", retry_after=timedelta(hours=2))).retry_after == 7200.0