"""建别名限速：最小间隔与 Apple 临时封禁取更晚的一条。"""

from datetime import UTC, datetime, timedelta

from icloud_api.rate_limit import AliasCreateRateLimiter

NOW = datetime(2026, 10, 12, 0, 0, 0, tzinfo=UTC)
INTERVAL = timedelta(minutes=3)


def test_first_creation_is_not_delayed() -> None:
    limiter = AliasCreateRateLimiter()
    assert limiter.retry_after("acc-1", NOW, INTERVAL) == timedelta(0)


def test_minimum_interval_blocks_until_it_elapses() -> None:
    limiter = AliasCreateRateLimiter()
    limiter.record_success("acc-1", NOW)

    assert limiter.retry_after("acc-1", NOW, INTERVAL) == INTERVAL
    assert limiter.retry_after("acc-1", NOW + timedelta(minutes=2), INTERVAL) == timedelta(minutes=1)
    assert limiter.retry_after("acc-1", NOW + INTERVAL, INTERVAL) == timedelta(0)


def test_temporary_block_wins_over_minimum_interval() -> None:
    limiter = AliasCreateRateLimiter()
    limiter.record_success("acc-1", NOW)
    limiter.block("acc-1", NOW, timedelta(minutes=30))

    assert limiter.retry_after("acc-1", NOW + timedelta(minutes=1), INTERVAL) == timedelta(minutes=29)
    # 封禁过期后回落到最小间隔规则，而不是直接放行
    assert limiter.retry_after("acc-1", NOW + timedelta(minutes=29), INTERVAL) == timedelta(minutes=1)
    assert limiter.retry_after("acc-1", NOW + timedelta(minutes=31), INTERVAL) == timedelta(0)


def test_block_keeps_the_later_deadline_and_ignores_non_positive() -> None:
    limiter = AliasCreateRateLimiter()
    limiter.block("acc-1", NOW, timedelta(minutes=10))
    limiter.block("acc-1", NOW, timedelta(minutes=5))
    assert limiter.retry_after("acc-1", NOW, INTERVAL) == timedelta(minutes=10)

    limiter.block("acc-1", NOW, timedelta(0))
    assert limiter.retry_after("acc-1", NOW, INTERVAL) == timedelta(minutes=10)


def test_accounts_are_tracked_separately() -> None:
    limiter = AliasCreateRateLimiter()
    limiter.record_success("acc-1", NOW)

    assert limiter.retry_after("acc-1", NOW, INTERVAL) == INTERVAL
    assert limiter.retry_after("acc-2", NOW, INTERVAL) == timedelta(0)


def test_zero_interval_only_respects_temporary_blocks() -> None:
    limiter = AliasCreateRateLimiter()
    limiter.record_success("acc-1", NOW)
    assert limiter.retry_after("acc-1", NOW, timedelta(0)) == timedelta(0)
