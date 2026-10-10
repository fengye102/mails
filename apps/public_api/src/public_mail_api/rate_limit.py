"""Small process-local sliding-window limiter for the public edge."""
from collections import defaultdict, deque
from time import monotonic


class RateLimitExceeded(Exception):
    pass


class RateLimiter:
    def __init__(self):
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, bucket: str, key: str, *, limit: int, window_seconds: int) -> None:
        now = monotonic()
        events = self._events[(bucket, key)]
        while events and events[0] <= now - window_seconds:
            events.popleft()
        if len(events) >= limit:
            raise RateLimitExceeded
        events.append(now)

    def check_create(self, ip: str) -> None:
        self.check("create", ip, limit=3, window_seconds=3600)

    def check_list(self, token_hash: str) -> None:
        self.check("list", token_hash, limit=30, window_seconds=60)

    def check_detail(self, token_hash: str) -> None:
        self.check("detail", token_hash, limit=60, window_seconds=60)
