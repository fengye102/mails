"""UTC handling shared by C-stage routes."""

from datetime import UTC, datetime


def as_utc(value: datetime | None) -> datetime | None:
    """Treat database timestamps without tzinfo as UTC for SQLite compatibility."""

    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
