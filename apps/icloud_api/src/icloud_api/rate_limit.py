"""建别名限速（对应 CYMail `internal/server/alias_rate_limiter.go`）。

两条约束取"更晚生效"的一条：管理员设置的最小间隔，以及 Apple 返回的临时封禁。
"""

import threading
from datetime import datetime, timedelta


class AliasCreateRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_success: dict[str, datetime] = {}
        self._blocked_until: dict[str, datetime] = {}

    def retry_after(self, account_id: str, now: datetime, minimum_interval: timedelta) -> timedelta:
        with self._lock:
            blocked_until = self._blocked_until.get(account_id)
            if blocked_until is not None and blocked_until > now:
                return blocked_until - now
            self._blocked_until.pop(account_id, None)

            if minimum_interval <= timedelta(0):
                return timedelta(0)
            last_success = self._last_success.get(account_id)
            if last_success is None:
                return timedelta(0)
            ready_at = last_success + minimum_interval
            if ready_at > now:
                return ready_at - now
            return timedelta(0)

    def record_success(self, account_id: str, now: datetime) -> None:
        with self._lock:
            self._last_success[account_id] = now

    def block(self, account_id: str, now: datetime, retry_after: timedelta) -> None:
        if retry_after <= timedelta(0):
            return
        with self._lock:
            until = now + retry_after
            current = self._blocked_until.get(account_id)
            if current is None or until > current:
                self._blocked_until[account_id] = until
