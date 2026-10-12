"""定时创建调度（对应 Go `scheduler.go`）。

一个任务绑定一个账号：目标轮次 + 间隔。`enabled` 默认关闭，必须由界面显式打开；
`target_count = 0` 表示不限轮次（与 Go 的 `Rounds <= 0` 一致）。

每轮只创建一个别名（Apple Account 优先，走 `alias_service.create_alias`），按结果决定：
创建成功 → 计数 +1、间隔后再来；总容量已满 → 自动停（再试也不会成功）；
被 Apple 限速 → 按建议时长退避；其他错误 → 记 `last_error` 后按间隔重来。

`run_round` 是同步可测的核心：创建动作通过 `create_fn` 注入，不在这里碰网络。
调用方（`SchedulerService`）负责查库、注入 `create_fn` 和 commit。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from icloud_api.accounts import resolve_key
from icloud_api.alias_service import CreateOutcome
from icloud_api.aliases import (
    AliasLimitReached,
    AliasRateLimited,
    alias_retry_after,
    root_alias_error,
)
from icloud_api.models import ICloudAccount, ICloudSchedulerJob
from icloud_api.rate_limit import AliasCreateRateLimiter
from icloud_api.settings import Settings

# 轮次最小间隔（对应 Go `schedulerMinInterval`）。
MIN_INTERVAL_SECONDS = 30
# 保留的最大事件条数（先进先出，对应 Go `schedulerMaxEvents`）。
MAX_EVENTS = 100
# 上游没给 Retry-After 时的默认退避。
DEFAULT_RATE_LIMIT_BACKOFF = timedelta(hours=1)

RoundStatus = Literal["idle", "not_due", "created", "done", "limit_reached", "rate_limited", "failed"]


@dataclass(frozen=True)
class RoundResult:
    """一轮的结果。`disable` 表示任务已被自动关闭。"""

    status: RoundStatus
    email: str = ""
    interface: str = ""
    detail: str = ""
    next_delay: timedelta | None = None
    disable: bool = False


def effective_interval(job: ICloudSchedulerJob) -> timedelta:
    """任务间隔下限取 `MIN_INTERVAL_SECONDS`（防止界面填 0 把账号打爆）。"""
    return timedelta(seconds=max(MIN_INTERVAL_SECONDS, job.interval_seconds))


def add_event(job: ICloudSchedulerJob, when: datetime, message: str) -> None:
    """追加一条事件，超过上限丢弃最旧的。`events` 落库是 JSON 列。"""
    events = list(job.events or [])
    events.append({"at": when.isoformat(), "message": message})
    job.events = events[-MAX_EVENTS:]


def reached_target(job: ICloudSchedulerJob) -> bool:
    return job.target_count > 0 and job.created_count >= job.target_count


def is_due(job: ICloudSchedulerJob, now: datetime) -> bool:
    """到点与否：没排过下一轮（刚建/刚开关）或已过下一轮时间。"""
    if not job.enabled:
        return False
    if reached_target(job):
        return True  # 需要一个轮次去收尾（关掉任务并留痕）
    if job.next_run_at is None:
        return True
    return _as_utc(job.next_run_at) <= now


def _as_utc(moment: datetime) -> datetime:
    """SQLite 取回的 datetime 可能没有时区，统一按 UTC 处理。"""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def run_round(
    job: ICloudSchedulerJob,
    *,
    now: datetime,
    create_fn: Callable[[], Awaitable[CreateOutcome]],
    limiter: AliasCreateRateLimiter | None = None,
) -> RoundResult:
    """跑一轮（不碰网络：创建动作由 `create_fn` 注入）。调用方负责 commit。"""
    if not job.enabled:
        return RoundResult(status="idle")
    interval = effective_interval(job)

    if reached_target(job):
        job.enabled = False
        job.next_run_at = None
        add_event(job, now, f"已达到目标数量 {job.target_count} 个，自动停止")
        return RoundResult(status="done", disable=True)

    if limiter is not None:
        wait = limiter.retry_after(job.account_id, now, interval)
        if wait > timedelta(0):
            job.next_run_at = now + wait
            return RoundResult(status="not_due", next_delay=wait)

    job.last_run_at = now
    try:
        outcome = await create_fn()
    except Exception as error:  # noqa: BLE001 - 分类后再决定：停 / 退避 / 记错误继续
        # 必须穿透包装层拿分类（上游客户端会重新包一层），否则「容量满」会被
        # 当成普通失败，任务就会一直对着已满的账号打到被封。
        root = root_alias_error(error)
        if isinstance(root, AliasLimitReached):
            # 账号级总容量：再试也不会成功，直接停。
            job.enabled = False
            job.next_run_at = None
            job.last_error = _detail(root, error)
            add_event(job, now, f"隐藏邮箱总容量已满，自动停止：{job.last_error}")
            return RoundResult(status="limit_reached", detail=job.last_error, disable=True)
        if isinstance(root, AliasRateLimited):
            delay = alias_retry_after(root)
            if delay <= timedelta(0):
                delay = DEFAULT_RATE_LIMIT_BACKOFF
            if limiter is not None:
                limiter.block(job.account_id, now, delay)
            job.next_run_at = now + delay
            job.last_error = _detail(root, error)
            add_event(job, now, f"被 Apple 限速，建议 {_humanize(delay)} 后重试")
            return RoundResult(status="rate_limited", detail=job.last_error, next_delay=delay)
        job.next_run_at = now + interval
        job.last_error = _detail(root, error)
        add_event(job, now, f"创建失败：{job.last_error}")
        return RoundResult(status="failed", detail=job.last_error, next_delay=interval)

    job.created_count += 1
    job.last_error = ""
    if limiter is not None:
        limiter.record_success(job.account_id, now)
    add_event(job, now, f"创建成功：{outcome.email}（接口 {outcome.interface}）")

    if reached_target(job):
        job.enabled = False
        job.next_run_at = None
        add_event(job, now, f"已达到目标数量 {job.target_count} 个，自动停止")
        return RoundResult(
            status="created",
            email=outcome.email,
            interface=outcome.interface,
            disable=True,
        )

    job.next_run_at = now + interval
    return RoundResult(
        status="created",
        email=outcome.email,
        interface=outcome.interface,
        next_delay=interval,
    )


def _humanize(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _detail(root: BaseException, original: BaseException) -> str:
    """记日志用的文案：优先用最具体那层的 message，否则退回外层。"""
    return getattr(root, "message", "") or str(root) or str(original)


class SchedulerService:
    """后台扫描器：按 tick 查找到点任务并各跑一轮。

    单循环而不是「每任务一个后台任务」：任务状态都在数据库里，
    重启后能接着跑，也不用维护一堆 cancel 句柄（Go 版用 goroutine + cancel）。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        create_alias: Callable[..., Awaitable[CreateOutcome]] | None = None,
        limiter: AliasCreateRateLimiter | None = None,
        tick_seconds: float = 15.0,
        transport: Any = None,
    ) -> None:
        if create_alias is None:
            # 延迟导入避免循环依赖（alias_service 不依赖 scheduler）。
            from icloud_api import alias_service

            create_alias = alias_service.create_alias
        self._session_factory = session_factory
        self._settings = settings
        self._create_alias = create_alias
        self._limiter = limiter if limiter is not None else AliasCreateRateLimiter()
        self._tick_seconds = max(1.0, tick_seconds)
        self._transport = transport
        self._stopped = True

    @property
    def key(self) -> bytes:
        return resolve_key(self._settings.data_key)

    async def tick(self, now: datetime | None = None) -> list[tuple[str, RoundResult]]:
        """扫描并处理所有到点任务。返回 (job_id, 结果) 列表；调用方可在外部触发。"""
        moment = now or datetime.now(UTC)
        results: list[tuple[str, RoundResult]] = []
        with self._session_factory() as db:
            jobs = list(
                db.execute(
                    select(ICloudSchedulerJob).where(ICloudSchedulerJob.enabled.is_(True))
                ).scalars()
            )
            for job in jobs:
                if not is_due(job, moment):
                    continue
                account = db.get(ICloudAccount, job.account_id)
                if account is None:
                    job.enabled = False
                    add_event(job, moment, "账号已删除，任务自动停止")
                    results.append((job.id, RoundResult(status="done", detail="账号已删除", disable=True)))
                    continue

                async def create_once(account: ICloudAccount = account) -> CreateOutcome:
                    return await self._create_alias(account, self.key, transport=self._transport)

                result = await run_round(
                    job, now=moment, create_fn=create_once, limiter=self._limiter
                )
                results.append((job.id, result))
            db.commit()
        return results

    async def run_forever(self, *, tick_seconds: float | None = None) -> None:
        """后台循环；由 app lifespan 以 asyncio task 拉起。"""
        import asyncio

        interval = tick_seconds if tick_seconds is not None else self._tick_seconds
        self._stopped = False
        while not self._stopped:
            try:
                await self.tick()
            except Exception as error:  # noqa: BLE001 - 调度器不能因为单个 tick 崩掉
                print(f"[scheduler] tick 失败: {error}")
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._stopped = True


def default_job_for_account(
    account_id: str,
    settings: Settings,
    *,
    target_count: int | None = None,
    interval_seconds: int | None = None,
) -> ICloudSchedulerJob:
    """按设置里的默认值新建任务。`enabled` 一律 False：必须由界面显式打开。

    `target_count = 0` 表示不限轮次（对齐 Go 的 `Rounds <= 0`）；
    默认取 `settings.alias_default_count`，避免默认就无限创建。
    """
    return ICloudSchedulerJob(
        account_id=account_id,
        enabled=False,
        target_count=settings.alias_default_count if target_count is None else target_count,
        interval_seconds=(
            settings.alias_min_interval_seconds if interval_seconds is None else interval_seconds
        ),
        created_count=0,
        last_error="",
        events=[],
    )


__all__ = [
    "MAX_EVENTS",
    "MIN_INTERVAL_SECONDS",
    "RoundResult",
    "SchedulerService",
    "add_event",
    "default_job_for_account",
    "effective_interval",
    "is_due",
    "reached_target",
    "run_round",
]
