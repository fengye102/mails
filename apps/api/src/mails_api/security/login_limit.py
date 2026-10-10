"""Single-worker failed-login buckets, keyed by username and direct peer IP."""

from collections import deque
from datetime import datetime, timedelta
from threading import Lock


class LoginLimiter:
    def __init__(self) -> None:
        self._failures: dict[tuple[str, str], deque[datetime]] = {}
        self._lock = Lock()

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=15)
        for key, timestamps in list(self._failures.items()):
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if not timestamps:
                del self._failures[key]

    def record_failure(self, username: str, peer_ip: str, now: datetime) -> None:
        with self._lock:
            self._prune(now)
            key = (username.casefold(), peer_ip)
            # Concurrent requests can complete out of timestamp order. Keep the
            # newest five sorted so pruning never leaves expired entries behind.
            timestamps = [*self._failures.get(key, ()), now]
            self._failures[key] = deque(sorted(timestamps)[-5:], maxlen=5)

    def is_limited(self, username: str, peer_ip: str, now: datetime) -> bool:
        with self._lock:
            self._prune(now)
            return len(self._failures.get((username.casefold(), peer_ip), ())) >= 5

    def reset(self, username: str, peer_ip: str) -> None:
        with self._lock:
            self._failures.pop((username.casefold(), peer_ip), None)
