"""INBOX 常驻监听（对应 CYMail `internal/mail/idle.go`）。

循环：(重)连接 → SELECT INBOX 取 UIDNEXT → 补拉/增量拉取并逐批回调 → IDLE 等待 → 重复。

稳态循环用 `STATUS INBOX (UIDNEXT)` 刷新游标，而不是反复 SELECT：SELECT 的
EXISTS/RECENT 未请求更新会自唤醒 IDLE，STATUS 没这个噪声。

游标语义与 Go 一致：都是「下一个待拉取的 UID」。
- `start_uid = 0`：只收之后到达的新邮件，不回补历史；
- `start_uid > 0`：首个周期从 `max(start_uid - cursor_overlap, 1)` 回扫重叠窗口
  （兜住陈旧外部游标边界上可能漏掉的邮件），回调里可能出现重复，由调用方按 ID 去重；
  此后内部游标精确维护，重连从上次游标继续，不再重复。
- 每批回调带 `next_uid`，调用方持久化它，下次作为 `start_uid` 传入即可精确续传。

回调抛错立即终止并把原错误抛回（重试策略由调用方决定）；网络异常按 1s→2s→4s…（封顶
`reconnect_max`）自动重连；`stop_event` 置位时优雅退出并返回收尾状态。
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

BASE_BACKOFF_SECONDS = 1.0
DEFAULT_RECONNECT_MAX_SECONDS = 30.0
DEFAULT_IDLE_TIMEOUT_SECONDS = 20 * 60
DEFAULT_CURSOR_OVERLAP = 100
DEFAULT_FETCH_CHUNK = 100
IDLE_WAIT_STEP_SECONDS = 1.0


class IdleHandlerError(Exception):
    """回调自身抛出的错误，用于和网络错误区分（网络错误会触发重连）。"""

    def __init__(self, error: BaseException) -> None:
        self.original = error
        super().__init__(f"mail: idle handler: {error}")


@dataclass(frozen=True)
class IdlePlan:
    """一次监听的全部参数（已填默认值）。"""

    start_uid: int = 0
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS
    reconnect_max: float = DEFAULT_RECONNECT_MAX_SECONDS
    cursor_overlap: int = DEFAULT_CURSOR_OVERLAP
    fetch_chunk: int = DEFAULT_FETCH_CHUNK


@dataclass(frozen=True)
class IdleResult:
    """退出时返回的收尾状态（stop 置位或回调前的网络失败重试次数用尽）。"""

    cursor: int
    batches: int
    reconnects: int


def plan_from(
    *,
    start_uid: int = 0,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    reconnect_max: float = DEFAULT_RECONNECT_MAX_SECONDS,
    cursor_overlap: int = DEFAULT_CURSOR_OVERLAP,
    fetch_chunk: int = DEFAULT_FETCH_CHUNK,
) -> IdlePlan:
    """填默认值：IDLE 单次 20 分钟、重连封顶 30s、回扫余量与分块宽度各 100。"""
    return IdlePlan(
        start_uid=max(int(start_uid), 0),
        idle_timeout=idle_timeout if idle_timeout > 0 else DEFAULT_IDLE_TIMEOUT_SECONDS,
        reconnect_max=reconnect_max if reconnect_max > 0 else DEFAULT_RECONNECT_MAX_SECONDS,
        cursor_overlap=cursor_overlap if cursor_overlap > 0 else DEFAULT_CURSOR_OVERLAP,
        fetch_chunk=fetch_chunk if fetch_chunk > 0 else DEFAULT_FETCH_CHUNK,
    )


def overlap_start(cursor: int, margin: int) -> int:
    """重叠回扫起点：游标减安全余量，下限 1。"""
    if cursor <= margin:
        return 1
    return cursor - margin


def chunk_end(first: int, uid_next: int, chunk: int) -> int:
    """[first, uid_next) 内当前块的结束 UID（含）。"""
    return min(first + chunk - 1, uid_next - 1)


def next_backoff(
    current: float,
    base: float = BASE_BACKOFF_SECONDS,
    maximum: float = DEFAULT_RECONNECT_MAX_SECONDS,
) -> float:
    """指数退避：current*2，封顶 maximum；current<=0 时取 min(base, maximum)。"""
    following = base if current <= 0 else current * 2
    if following <= 0 or following > maximum:
        return maximum
    return following


def sync_range(
    client,
    start: int,
    uid_next: int,
    on_batch: Callable[[list, int], object],
    *,
    chunk: int = DEFAULT_FETCH_CHUNK,
) -> int:
    """拉取 [start, uid_next)，按块逐批回调；返回处理完后的游标（正常等于 uid_next）。

    回调抛错包成 `IdleHandlerError` 抛出，与网络错误区分开。
    """
    cursor = start
    while cursor < uid_next:
        last = chunk_end(cursor, uid_next, chunk)
        messages = client.fetch_uid_range(cursor, last)
        cursor = last + 1
        if messages:
            try:
                on_batch(messages, cursor)
            except Exception as error:
                raise IdleHandlerError(error) from error
    return cursor


def idle_watch_inbox(
    client,
    *,
    on_batch: Callable[[list, int], object],
    start_uid: int = 0,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    reconnect_max: float = DEFAULT_RECONNECT_MAX_SECONDS,
    cursor_overlap: int = DEFAULT_CURSOR_OVERLAP,
    fetch_chunk: int = DEFAULT_FETCH_CHUNK,
    stop_event: threading.Event | None = None,
    sleep: Callable[[float], bool] | None = None,
    on_reconnect: Callable[[float], None] | None = None,
) -> IdleResult:
    """常驻监听 INBOX。`sleep(seconds)` 返回 False 表示收到停止信号（测试注入用）。"""
    if on_batch is None:
        raise ValueError("mail: idle_watch_inbox 的 on_batch 不能为 None")
    plan = plan_from(
        start_uid=start_uid,
        idle_timeout=idle_timeout,
        reconnect_max=reconnect_max,
        cursor_overlap=cursor_overlap,
        fetch_chunk=fetch_chunk,
    )
    stop = stop_event or threading.Event()
    wait = sleep or _default_sleep(stop)

    cursor = plan.start_uid
    # 重叠回扫只在首个同步周期发生，且只有调用方传入的外部游标可能陈旧。
    overlap_on_backfill = plan.start_uid > 0
    first_after_connect = True
    backoff = min(BASE_BACKOFF_SECONDS, plan.reconnect_max)
    batches = 0
    reconnects = 0

    def recover() -> bool:
        """掉线后收尾：断开连接、等待退避、推进退避值。返回 False 表示该收工了。"""
        nonlocal backoff, reconnects
        _drop(client)
        if not wait(backoff):
            return False
        backoff = next_backoff(backoff, BASE_BACKOFF_SECONDS, plan.reconnect_max)
        reconnects += 1
        if on_reconnect is not None:
            on_reconnect(backoff)
        return True

    try:
        while not stop.is_set():
            if getattr(client, "connection", None) is None:
                try:
                    client.connect()
                except Exception:  # noqa: BLE001 - 连接类失败统一走退避重连
                    if stop.is_set() or not recover():
                        return IdleResult(cursor, batches, reconnects)
                    continue
                backoff = BASE_BACKOFF_SECONDS
                first_after_connect = True

            if first_after_connect:
                try:
                    _, uid_next = client.select_inbox(readonly=True)
                except Exception:  # noqa: BLE001
                    if not recover():
                        return IdleResult(cursor, batches, reconnects)
                    continue
                if cursor == 0:
                    cursor = uid_next  # 自动起点：从当前 UIDNEXT 开始，不回补历史
                cursor = min(cursor, uid_next)  # 防御：服务器 UIDNEXT 回退（正常不会发生）
                fetch_start = overlap_start(cursor, plan.cursor_overlap) if overlap_on_backfill else cursor
                first_after_connect = False
                if fetch_start < uid_next:
                    try:
                        cursor, produced = _sync(client, fetch_start, uid_next, on_batch, plan.fetch_chunk)
                    except IdleHandlerError:
                        raise
                    except Exception:  # noqa: BLE001
                        # 保持重叠标记：重连后重试仍带重叠。
                        if not recover():
                            return IdleResult(cursor, batches, reconnects)
                        continue
                    batches += produced
                    if stop.is_set():
                        return IdleResult(cursor, batches, reconnects)
                overlap_on_backfill = False
                backoff = BASE_BACKOFF_SECONDS
            else:
                # 稳态：STATUS 刷新 UIDNEXT（无未请求更新噪声）。
                try:
                    uid_next = client.uid_next()
                except Exception:  # noqa: BLE001
                    if not recover():
                        return IdleResult(cursor, batches, reconnects)
                    continue
                if uid_next > cursor:
                    try:
                        cursor, produced = _sync(client, cursor, uid_next, on_batch, plan.fetch_chunk)
                    except IdleHandlerError:
                        raise
                    except Exception:  # noqa: BLE001
                        if not recover():
                            return IdleResult(cursor, batches, reconnects)
                        continue
                    batches += produced
                    if stop.is_set():
                        return IdleResult(cursor, batches, reconnects)
                backoff = BASE_BACKOFF_SECONDS

            try:
                _idle_once(client, plan.idle_timeout, stop)
            except Exception:  # noqa: BLE001 - IDLE 断线走重连
                if stop.is_set() or not recover():
                    return IdleResult(cursor, batches, reconnects)
                continue
            if stop.is_set():
                return IdleResult(cursor, batches, reconnects)
    finally:
        _drop(client)
    return IdleResult(cursor, batches, reconnects)


def _sync(client, start: int, uid_next: int, on_batch, chunk: int) -> tuple[int, int]:
    produced = 0

    def batch(messages: list, next_uid: int) -> None:
        nonlocal produced
        on_batch(messages, next_uid)
        produced += 1

    return sync_range(client, start, uid_next, batch, chunk=chunk), produced


def _drop(client) -> None:
    try:
        client.disconnect()
    except Exception:
        logging.getLogger(__name__).debug("IDLE 断开旧连接失败（忽略）", exc_info=True)


def _idle_once(client, timeout: float, stop: threading.Event) -> None:
    """一次 IDLE：等到新邮件通知或超时就返回；断线抛错交给调用方重连。"""
    tag = client.start_idle()
    try:
        remaining = timeout
        while remaining > 0:
            step = min(IDLE_WAIT_STEP_SECONDS, remaining)
            if stop.wait(step):
                return
            if client.wait_idle_activity(step):
                return
            remaining -= step
    finally:
        try:
            client.stop_idle(tag)
        except Exception:
            logging.getLogger(__name__).debug("IDLE DONE 发送失败（连接可能已断，忽略）", exc_info=True)


def _default_sleep(stop: threading.Event) -> Callable[[float], bool]:
    def sleep(seconds: float) -> bool:
        return not stop.wait(seconds)

    return sleep
