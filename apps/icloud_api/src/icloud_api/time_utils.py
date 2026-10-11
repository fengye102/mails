"""UTC 处理（与 C 的 `delivery_api.time_utils` 同一约定）。

SQLite 读回来的时间戳没有 tzinfo，直接和带时区的 `now` 比较会抛
"can't compare offset-naive and offset-aware datetimes"；比较前一律先过 `as_utc`。
"""

from datetime import UTC, datetime


def as_utc(value: datetime | None) -> datetime | None:
    """把数据库里没有 tzinfo 的时间戳按 UTC 解读（SQLite 兼容）。"""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
