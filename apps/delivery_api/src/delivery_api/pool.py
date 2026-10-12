"""邮箱池（对外供注册机）：承接 OEP 对外契约的底盘。

契约基准是 OEP（outlook-email-plus）的 ``/api/external/pool/*``：注册机按它写死了路径、
字段与错误码，所以这里照抄对外形状，只把底座从 OEP 的账号表换成平台库存
（``inventory_mailboxes`` 库存行 + ``pool_claims`` 领取留痕）。

三条必须原样保留、错了注册机就接不上的约定：

1. 响应信封固定 ``{"success", "code", "message", "data"}``。
2. 错误码出网前**转大写**；``NO_AVAILABLE_ACCOUNT`` 是 **HTTP 200 + success=false**，
   这是「池里没有可用邮箱」的约定信号——改成 404/409 会让注册机走错分支。
3. ``account_id`` 是整数。平台上它是 ``pool_claims.id``（领取记录的自增 id），
   不是邮箱的 UUID：注册机对这个值只做「领取时拿到、释放/回传时回传」，
   直接外露 UUID 会破坏调用方的 ``int(account_id)``。

池的当前状态落在 ``inventory_mailboxes.pool_status`` 上：领取必须是**一条 UPDATE** 完成
（20 线程争同一个邮箱只能有一个赢家），状态留在库存行上才做得到；``pool_claims``
是过程留痕（谁、何时、拿了哪个邮箱、结果如何），不作为并发闸门。
状态机照 OEP 的 ``RESULT_TO_POOL_STATUS``：

    available - claim   -> claimed
    claimed   - release -> available
    claimed   - complete(success)             -> used      （不再发）
    claimed   - complete(verification_timeout)-> cooldown  - 冷却到期 -> available
    claimed   - complete(provider_blocked)    -> frozen    （不再发）
    claimed   - complete(credential_invalid)  -> retired   （不再发）
    claimed   - complete(network_error)       -> available
    claimed   - 租约到期（回收）              -> cooldown  - 冷却到期 -> available
"""

from __future__ import annotations

import base64
import os
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import and_, false, func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from delivery_api.models import (
    DeliveryAuditEvent,
    InventoryMailbox,
    PoolApiKey,
    PoolClaim,
    utc_now,
)
from delivery_api.security import hash_secret, new_code
from delivery_api.settings import Settings
from delivery_api.time_utils import as_utc

# ------------------------------------------------------------------ 状态与常量

POOL_AVAILABLE = "available"
POOL_CLAIMED = "claimed"
POOL_USED = "used"
POOL_COOLDOWN = "cooldown"
POOL_FROZEN = "frozen"
POOL_RETIRED = "retired"
POOL_STATUSES = (POOL_AVAILABLE, POOL_CLAIMED, POOL_USED, POOL_COOLDOWN, POOL_FROZEN, POOL_RETIRED)
# 运维手工可以设置的状态：其余状态由流程推进，手工置 claimed/used 只会和租约打架。
OPS_SETTABLE_STATUSES = (POOL_AVAILABLE, POOL_FROZEN, POOL_RETIRED)

CLAIM_CLAIMED = "claimed"
CLAIM_COMPLETED = "completed"
CLAIM_RELEASED = "released"
CLAIM_EXPIRED = "expired"

# 回传结果 → 池状态，照抄 OEP ``repositories/pool.RESULT_TO_POOL_STATUS``。
RESULT_TO_POOL_STATUS = {
    "success": POOL_USED,
    "verification_timeout": POOL_COOLDOWN,
    "provider_blocked": POOL_FROZEN,
    "credential_invalid": POOL_RETIRED,
    "network_error": POOL_AVAILABLE,
}
VALID_RESULTS = tuple(RESULT_TO_POOL_STATUS)

# OEP 的 provider 词表（注册机按这个取值传）。
VALID_PROVIDERS = ("outlook", "imap", "custom", "gptmail", "cloudflare_temp_mail")
# OEP 词表 → 平台来源标签（A 站 provider kind / 库存 source_label）。
# custom / gptmail 是 OEP 自带的临时邮箱源，平台没有对应物：取值合法，但永不命中，
# 返回 NO_AVAILABLE_ACCOUNT（「池里没有可用邮箱」）比返回 400 更接近事实。
PROVIDER_SOURCES: dict[str, tuple[str, ...]] = {
    "outlook": ("outlook_plus",),
    "imap": ("cymail", "icloud_hme"),
    "cloudflare_temp_mail": ("cloudflare_temp_email",),
    "custom": (),
    "gptmail": (),
}
# 池只发「有邮件来源可读」的邮箱（规格不变量 5）：这些来源平台确实取得到邮件
# （A 站四个只读来源 kind + 引擎同步用的标签）。其余 label（历史遗留、手写）不进池，
# 免得发出去一个谁也读不到信的地址。可用 settings.pool_source_labels 覆盖。
READABLE_SOURCE_LABELS = ("cloudflare_temp_email", "cymail", "outlook_plus", "icloud_hme", "icloud-hme")

CALLER_ID_MAX_LEN = 64
TASK_ID_MAX_LEN = 128
PROJECT_KEY_MAX_LEN = 128
EMAIL_DOMAIN_MAX_LEN = 128
REASON_MAX_LEN = 256
DETAIL_MAX_LEN = 512
IDEMPOTENCY_KEY_MAX_LEN = 128

POOL_KEY_PREFIX = "pool_"

# 审计事件名（与 C 站既有的 ``event_type`` 同表）。
EVENT_API_ACCESS = "pool_api_access"
EVENT_KEY_CREATED = "pool_key_created"
EVENT_KEY_UPDATED = "pool_key_updated"
EVENT_MAILBOX_STATUS = "pool_mailbox_status_changed"
EVENT_CLAIMED = "pool_claimed"
EVENT_RELEASED = "pool_released"
EVENT_COMPLETED = "pool_completed"
EVENT_LEASE_EXPIRED = "pool_lease_expired"


# ------------------------------------------------------------------ 对外信封


def public_code(code: str | None) -> str:
    """错误码出网统一大写（OEP 的 ``_pool_error_code`` 也是 ``.upper()``）。"""

    return str(code or "INTERNAL_ERROR").upper()


def ok(data: Any = None, message: str = "success") -> dict[str, Any]:
    return {"success": True, "code": "OK", "message": message, "data": data}


def fail(code: str, message: str, *, data: Any = None) -> dict[str, Any]:
    return {"success": False, "code": public_code(code), "message": message, "data": data}


class PoolError(Exception):
    """池业务错误：带对外错误码、HTTP 状态码与可选 data（OEP 的 fail 也有 data）。"""

    def __init__(self, message: str, error_code: str, http_status: int = 400, *, data: Any = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.http_status = http_status
        self.data = data


# ------------------------------------------------------------------ 入参校验
# 长度与错误码逐个对齐 OEP ``services/pool.py`` 的 ``_validate_*``：注册机按码分支。


def validate_caller_id(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        raise PoolError("caller_id 不能为空", "caller_id_empty")
    if len(text) > CALLER_ID_MAX_LEN:
        raise PoolError(f"caller_id 超过最大长度 {CALLER_ID_MAX_LEN}", "caller_id_too_long")
    return text


def validate_task_id(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        raise PoolError("task_id 不能为空", "task_id_empty")
    if len(text) > TASK_ID_MAX_LEN:
        raise PoolError(f"task_id 超过最大长度 {TASK_ID_MAX_LEN}", "task_id_too_long")
    return text


def validate_project_key(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) > PROJECT_KEY_MAX_LEN:
        raise PoolError(f"project_key 超过最大长度 {PROJECT_KEY_MAX_LEN}", "project_key_too_long")
    return text


def validate_email_domain(value: str | None) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if len(text) > EMAIL_DOMAIN_MAX_LEN:
        raise PoolError(f"email_domain 超过最大长度 {EMAIL_DOMAIN_MAX_LEN}", "email_domain_too_long")
    return text


def validate_provider(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if text not in VALID_PROVIDERS:
        raise PoolError(f"provider 必须是 {', '.join(VALID_PROVIDERS)} 之一", "invalid_provider")
    return text


def validate_result(value: str | None) -> str:
    text = (value or "").strip()
    if text not in VALID_RESULTS:
        raise PoolError(f"result 必须是 {', '.join(VALID_RESULTS)} 之一", "invalid_result")
    return text


def validate_text(value: str | None, *, field: str, max_len: int) -> str:
    text = value or ""
    if len(text) > max_len:
        raise PoolError(f"{field} 超过最大长度 {max_len}", f"{field}_too_long")
    return text


def validate_lease_seconds(value: int | None, *, settings: Settings) -> int:
    seconds = settings.pool_default_lease_seconds if value is None else int(value)
    if seconds <= 0:
        raise PoolError("lease_seconds 必须大于 0", "lease_seconds_invalid")
    if seconds > settings.pool_max_lease_seconds:
        raise PoolError(f"lease_seconds 不能超过 {settings.pool_max_lease_seconds} 秒", "lease_seconds_too_long")
    return seconds


# ------------------------------------------------------------------ 池密钥


def new_pool_key() -> str:
    """池密钥：``pool_`` + 32 字节 base64url。明文只在创建响应里出现一次。"""

    return POOL_KEY_PREFIX + base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def create_pool_key(
    db: Session,
    *,
    name: str,
    project: str = "",
    allowed_domains: list[str] | None = None,
    rate_limit_per_minute: int | None = None,
    enabled: bool = True,
    settings: Settings,
) -> tuple[PoolApiKey, str]:
    """建 Key 并返回 (行, 明文)。明文只经这一次返回值出网，库里只有摘要。"""

    raw = new_pool_key()
    row = PoolApiKey(
        name=name.strip(),
        project=project.strip(),
        key_hash=hash_secret(raw),
        enabled=enabled,
        allowed_domains=sorted({d.strip().lower() for d in (allowed_domains or []) if d.strip()}),
        rate_limit_per_minute=(
            settings.pool_default_rate_limit_per_minute if rate_limit_per_minute in (None, 0) else rate_limit_per_minute
        ),
    )
    db.add(row)
    db.commit()
    return row, raw


def extract_key(request: Request) -> str:
    """取调用方 Key。

    OEP 只认 ``X-API-Key``，E 规格写的是 ``Authorization: Bearer``；两个都认才叫
    「注册机零改动迁移」，因此这里按 Bearer 优先、X-API-Key 兜底。
    """

    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return token or (request.headers.get("x-api-key") or "").strip()


def authenticate_pool_key(db: Session, request: Request) -> PoolApiKey:
    """校验池密钥，失败一律 401 UNAUTHORIZED（OEP 的 ``api_key_required`` 同码）。"""

    raw = extract_key(request)
    if not raw:
        raise PoolError("API Key 缺失或无效", "unauthorized", http_status=401)
    if db.scalar(select(func.count()).select_from(PoolApiKey)) == 0:
        # 一个 Key 都没配：这是「池还没接好」，不是「你的 Key 错了」。
        # OEP 用 403 API_KEY_NOT_CONFIGURED 区分这两种情况，这里照搬。
        raise PoolError("池未配置任何 API Key", "api_key_not_configured", http_status=403)
    # 直接按 sha256 摘要查库：256 位随机密钥下这就是常量时间无关的等价比较，
    # 不需要额外再比一次摘要（那样只是看起来安全）。
    row = db.scalar(select(PoolApiKey).where(PoolApiKey.key_hash == hash_secret(raw)))
    if row is None or not row.enabled:
        # 停用的 Key 与不存在的 Key 返回同一个码：不向外区分「Key 存在但被停用」。
        raise PoolError("API Key 缺失或无效", "unauthorized", http_status=401)
    return row


def ensure_pool_enabled(settings: Settings) -> None:
    if not settings.pool_enabled:
        raise PoolError(
            "功能 external_pool 当前未启用",
            "feature_disabled",
            http_status=403,
            data={"feature": "external_pool"},
        )


def effective_project(key: PoolApiKey, requested: str) -> str:
    """Key 绑了项目就以 Key 为准；请求里的 project_key 与它不一致直接拒（项目隔离）。"""

    if key.project:
        if requested and requested != key.project:
            raise PoolError("project_key 与 API Key 归属项目不一致", "project_mismatch", http_status=403)
        return key.project
    return requested


def ensure_domain_allowed(key: PoolApiKey, domain: str) -> None:
    allowed = list(key.allowed_domains or [])
    if domain and allowed and domain not in allowed:
        raise PoolError(f"域名 {domain} 不在该 API Key 的允许范围内", "domain_not_allowed", http_status=403)


# ------------------------------------------------------------------ 限流


class RateLimiter:
    """进程内滑动窗口。

    只防单个 Key 把池刷爆，不承担配额语义，因此不做跨进程共享——池部署是单进程
    uvicorn，多 worker 时每个 worker 各算一份（已知偏差，写在这里而不是假装准确）。
    """

    def __init__(self, *, window_seconds: int = 60) -> None:
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def allow(self, bucket: str, limit: int, *, now: float | None = None) -> bool:
        if limit <= 0:
            return True
        moment = time.monotonic() if now is None else now
        hits = self._hits.setdefault(bucket, deque())
        cutoff = moment - self._window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(moment)
        return True


def enforce_rate_limit(limiter: RateLimiter, key: PoolApiKey) -> None:
    if not limiter.allow(f"pool-key:{key.id}", key.rate_limit_per_minute):
        raise PoolError("请求过于频繁", "rate_limited", http_status=429)


# ------------------------------------------------------------------ 可用性判据


def _normalized_label(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _normalized_source(column: Any) -> Any:
    """来源标签归一：忽略大小写与 ``-``/``_`` 差异（``icloud-hme`` 与 ``icloud_hme`` 是同一个来源）。"""

    return func.replace(func.lower(column), "-", "_")


def readable_sources(settings: Settings) -> tuple[str, ...]:
    configured = [_normalized_label(part) for part in (settings.pool_source_labels or "").split(",") if part.strip()]
    return tuple(configured) if configured else tuple(_normalized_label(x) for x in READABLE_SOURCE_LABELS)


def eligible_conditions(settings: Settings, now: datetime) -> list[Any]:
    """池「现在能不能把这个邮箱发出去」的全部条件——领取与 stats 共用这一份定义。

    领取与统计若各写一份条件，迟早出现「stats 说有可用、领取却一个都拿不到」。
    """

    return [
        InventoryMailbox.pool_status == POOL_AVAILABLE,
        # 库存侧说它可用：D5 同步会把上游停用/消失的行置 inactive/missing，
        # 运营手工置 disabled 的也不许发（与取件发信同一口径）。
        InventoryMailbox.status == "available",
        # 库存行的自身有效期过了就不发。
        or_(InventoryMailbox.expires_at.is_(None), InventoryMailbox.expires_at > now),
        # 来源可读（规格不变量 5）。
        _normalized_source(InventoryMailbox.source_label).in_(readable_sources(settings)),
    ]


def usage_conditions(*, caller_id: str, project: str) -> list[Any]:
    """「同 caller + 项目只防 success」（OEP v22 语义）。

    邮箱是做过一次就不再给同一个 caller 的同一个项目（防止把同一个地址重复送去注册），
    但换一个项目/换一个 caller 仍可用——所以这条不是全局排除，得带 caller+project。
    """

    if not (caller_id and project):
        return []
    return [
        ~select(PoolClaim.id)
        .where(
            PoolClaim.mailbox_id == InventoryMailbox.id,
            PoolClaim.caller_id == caller_id,
            PoolClaim.project == project,
            PoolClaim.result == "success",
        )
        .exists()
    ]


def source_conditions(provider: str | None) -> list[Any]:
    if provider is None:
        return []
    labels = [_normalized_label(x) for x in PROVIDER_SOURCES[provider]]
    if not labels:
        # OEP 词表里合法、平台没有对应来源（custom/gptmail）：条件是恒假，
        # 于是走「池里没有可用邮箱」，而不是报参数错。
        return [false()]
    return [_normalized_source(InventoryMailbox.source_label).in_(labels)]


def domain_conditions(domain: str) -> list[Any]:
    if not domain:
        return []
    # 地址里的域名在最后一段，锚在结尾比 LIKE '%domain%' 精确（不会被 x@notdomain 命中）。
    return [func.lower(InventoryMailbox.address).like(f"%@{domain}")]


def mailbox_eligible(db: Session, mailbox: InventoryMailbox, settings: Settings, now: datetime) -> bool:
    """单行版的可用性判定（运维接口与诊断用），判据与 ``eligible_conditions`` 同一份。"""

    conditions = and_(*eligible_conditions(settings, now))
    return bool(db.scalar(select(conditions).select_from(InventoryMailbox).where(InventoryMailbox.id == mailbox.id)))


# ------------------------------------------------------------------ 统计与审计


def pool_stats(db: Session, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    """池状态计数。``pool_counts`` 的六个键严格对齐 OEP ``get_stats``。"""

    current = now or utc_now()
    counts = dict.fromkeys(POOL_STATUSES, 0)
    rows = db.execute(
        select(InventoryMailbox.pool_status, func.count()).group_by(InventoryMailbox.pool_status)
    ).all()
    for status, total in rows:
        if status in counts:
            counts[status] = total
    # 诊断项（OEP 没有，附加不破坏兼容）：标记成 available、但库存侧已经不可用的行数。
    # 没有它就会出现「stats 说 available 有 5 个、领取却一个都拿不到」的自相矛盾。
    unusable = db.scalar(
        select(func.count())
        .select_from(InventoryMailbox)
        .where(
            InventoryMailbox.pool_status == POOL_AVAILABLE,
            ~and_(*eligible_conditions(settings, current)),
        )
    )
    return {"pool_counts": counts, "unusable_available": unusable or 0}


def audit(db: Session, *, event_type: str, details: dict[str, Any] | None = None, mailbox_id: str | None = None) -> None:
    """写一条审计事件。调用方负责它所在的提交边界。"""

    db.add(DeliveryAuditEvent(event_type=event_type, mailbox_id=mailbox_id, details=details or {}))


def lease_expiry(now: datetime, seconds: int) -> datetime:
    return now + timedelta(seconds=seconds)


def cooldown_deadline(now: datetime, settings: Settings) -> datetime:
    return now + timedelta(seconds=settings.pool_cooldown_seconds)


# ------------------------------------------------------------------ 领取 / 释放 / 回收

# 抢输一轮就换一个候选再试；轮数够覆盖「热门邮箱被并发抢走」的常见抖动，
# 又不至于在池真的空时把请求拖长。
CLAIM_ATTEMPTS = 8


def _isoformat(value: datetime | None) -> str:
    """对外时间统一 ISO 8601 + ``Z``（OEP 的 ``lease_expires_at`` 也是这个形状）。"""

    moment = as_utc(value)
    if moment is None:
        return ""
    return moment.isoformat().replace("+00:00", "Z")


def address_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def allowed_domain_conditions(allowed_domains: list[str] | None) -> list[Any]:
    """Key 的域名白名单：白名单为空 = 不限，不产生条件。"""

    domains = sorted({d.strip().lower() for d in (allowed_domains or []) if d.strip()})
    if not domains:
        return []
    return [or_(*[func.lower(InventoryMailbox.address).like(f"%@{domain}") for domain in domains])]


def expire_stale_claims(db: Session, settings: Settings, now: datetime | None = None) -> int:
    """回收租约到期的领取：claim → ``expired``，邮箱 → ``cooldown``（规格不变量 2）。

    时间比较在 Python 侧做，与 ``cleanup.py`` 同一口径：SQLite 存的是时间文本，
    在 SQL 里比大小是字符串比较，容易栽在「带不带 ``+00:00``」上。
    """

    current = now or utc_now()
    stale = []
    for claim in db.scalars(select(PoolClaim).where(PoolClaim.status == CLAIM_CLAIMED)).all():
        lease = as_utc(claim.lease_expires_at)
        if lease is not None and lease <= current:
            stale.append(claim)
    for claim in stale:
        claim.status = CLAIM_EXPIRED
        claim.completed_at = current
        mailbox = db.get(InventoryMailbox, claim.mailbox_id)
        if mailbox is not None and mailbox.pool_status == POOL_CLAIMED:
            # 回收后进冷却、不立刻再发：否则一次租约超时就能让同一个地址被反复发出去。
            mailbox.pool_status = POOL_COOLDOWN
            mailbox.pool_cooldown_until = cooldown_deadline(current, settings)
            mailbox.pool_lease_expires_at = None
        audit(
            db,
            event_type=EVENT_LEASE_EXPIRED,
            mailbox_id=claim.mailbox_id,
            details={"account_id": claim.id, "caller_id": claim.caller_id, "task_id": claim.task_id},
        )
    return len(stale)


def release_finished_cooldowns(db: Session, settings: Settings, now: datetime | None = None) -> int:
    """冷却到期 → 回到 ``available``。"""

    current = now or utc_now()
    ready = []
    for mailbox in db.scalars(select(InventoryMailbox).where(InventoryMailbox.pool_status == POOL_COOLDOWN)).all():
        deadline = as_utc(mailbox.pool_cooldown_until)
        if deadline is not None and deadline <= current:
            ready.append(mailbox)
    for mailbox in ready:
        mailbox.pool_status = POOL_AVAILABLE
        mailbox.pool_cooldown_until = None
    return len(ready)


def normalise_pool(db: Session, settings: Settings, now: datetime | None = None) -> dict[str, int]:
    """读路径与定时任务共用的自愈：先回收过期租约，再让到期的冷却归位。"""

    current = now or utc_now()
    expired = expire_stale_claims(db, settings, current)
    cooled = release_finished_cooldowns(db, settings, current)
    if expired or cooled:
        db.commit()
    return {"expired": expired, "cooled": cooled}


def _claimable_conditions(
    settings: Settings,
    *,
    caller_id: str,
    project: str,
    provider: str | None,
    email_domain: str,
    allowed_domains: list[str] | None,
    now: datetime,
) -> list[Any]:
    """领取候选的**唯一**判据集合（领取 / stats / 运维诊断都从这里取，避免条件漂移）。"""

    return [
        *eligible_conditions(settings, now),
        *source_conditions(provider),
        *domain_conditions(email_domain),
        *allowed_domain_conditions(allowed_domains),
        *usage_conditions(caller_id=caller_id, project=project),
    ]


def _claim_payload(claim: PoolClaim, mailbox: InventoryMailbox) -> dict[str, Any]:
    """OEP claim 响应的 ``data`` 字段集，一个不多一个不少。"""

    return {
        "account_id": claim.id,
        "email": mailbox.address,
        "email_domain": address_domain(mailbox.address),
        "claim_token": claim.claim_token,
        "claimed_at": _isoformat(claim.claimed_at),
        "lease_expires_at": _isoformat(claim.lease_expires_at),
    }


def _replay_claim(db: Session, idempotency_key: str, now: datetime) -> dict[str, Any] | None:
    """幂等重放：同一个 ``idempotency_key`` 再来一次，拿回**同一条**领取、不再消耗邮箱。

    只有还活着的租约能重放；已结束的领取重放明确报 409，而不是偷偷再发一个邮箱出去。
    """

    claim = db.scalar(select(PoolClaim).where(PoolClaim.idempotency_key == idempotency_key))
    if claim is None:
        return None
    lease = as_utc(claim.lease_expires_at)
    if claim.status != CLAIM_CLAIMED or lease is None or lease <= now:
        raise PoolError("该幂等键对应的领取已结束", "idempotency_conflict", http_status=409)
    mailbox = db.get(InventoryMailbox, claim.mailbox_id)
    if mailbox is None:
        raise PoolError("该幂等键对应的邮箱已不在库存里", "idempotency_conflict", http_status=409)
    return _claim_payload(claim, mailbox)


def claim_mailbox(
    db: Session,
    settings: Settings,
    *,
    caller_id: str,
    task_id: str,
    provider: str | None,
    project: str,
    email_domain: str,
    lease_seconds: int,
    allowed_domains: list[str] | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """原子领取一个邮箱。

    并发闸门是**一条 UPDATE**：``WHERE id = (SELECT ... ORDER BY RANDOM() LIMIT 1)
    AND pool_status = 'available'``。20 个线程同时来，只有一个能把那一行从 ``available``
    改成 ``claimed``（SQLite 写事务串行），抢输的换一个候选重试；候选挑不到就是
    「池里没有可用邮箱」——**HTTP 200 + success=false**，不是错误码。
    """

    current = now or utc_now()
    normalise_pool(db, settings, current)

    if idempotency_key:
        replayed = _replay_claim(db, idempotency_key, current)
        if replayed is not None:
            return replayed

    conditions = _claimable_conditions(
        settings,
        caller_id=caller_id,
        project=project,
        provider=provider,
        email_domain=email_domain,
        allowed_domains=allowed_domains,
        now=current,
    )
    lease_expires_at = lease_expiry(current, lease_seconds)

    for _ in range(CLAIM_ATTEMPTS):
        candidate = db.scalar(
            select(InventoryMailbox.id).where(*conditions).order_by(func.random()).limit(1)
        )
        if candidate is None:
            break
        try:
            won = db.execute(
                update(InventoryMailbox)
                .where(InventoryMailbox.id == candidate, InventoryMailbox.pool_status == POOL_AVAILABLE)
                .values(
                    pool_status=POOL_CLAIMED,
                    pool_lease_expires_at=lease_expires_at,
                    pool_cooldown_until=None,
                )
            ).rowcount
        except OperationalError:
            # SQLite 写并发会抛 database is locked/busy：事务已经不可用，
            # 回滚后换一个候选重试，比把 500 丢给注册机强。
            db.rollback()
            continue
        if not won:
            # 被别人抢先改成 claimed 了，换一个候选。
            continue

        mailbox = db.get(InventoryMailbox, candidate)
        assert mailbox is not None  # 刚由上面的 UPDATE 命中，必然存在
        claim = PoolClaim(
            mailbox_id=candidate,
            caller_id=caller_id,
            task_id=task_id,
            project=project,
            claim_token=new_code(),
            status=CLAIM_CLAIMED,
            claimed_at=current,
            lease_expires_at=lease_expires_at,
            idempotency_key=idempotency_key,
        )
        db.add(claim)
        db.flush()  # 取回自增主键，它就是对外契约里的 account_id
        audit(
            db,
            event_type=EVENT_CLAIMED,
            mailbox_id=candidate,
            details={
                "account_id": claim.id,
                "caller_id": caller_id,
                "task_id": task_id,
                "project": project,
                "email": mailbox.address,
                "lease_seconds": lease_seconds,
            },
        )
        db.commit()
        return _claim_payload(claim, mailbox)

    db.rollback()
    raise PoolError("池中没有符合条件的可用邮箱", "no_available_account", http_status=200)


def validate_claim_token(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        raise PoolError("claim_token 不能为空", "claim_token_empty")
    return text


def coerce_account_id(value: Any) -> int:
    """``account_id`` 必须是整数（OEP 在控制器里就 ``int(...)``）。"""

    if value is None:
        raise PoolError("account_id 不能为空", "account_id_missing", http_status=400)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PoolError("account_id 必须为整数", "account_id_invalid", http_status=400) from exc


def load_claim(db: Session, account_id: int) -> PoolClaim:
    claim = db.get(PoolClaim, account_id)
    if claim is None:
        raise PoolError("账号不存在", "account_not_found", http_status=400)
    return claim


def _validate_claim_ownership(
    claim: PoolClaim,
    *,
    action: str,
    claim_token: str,
    caller_id: str,
    task_id: str,
    project: str,
) -> None:
    """释放/回传的归属校验，错误码逐个对齐 OEP ``_validate_claim_ownership``。"""

    if claim.status != CLAIM_CLAIMED:
        raise PoolError(f"账号当前状态为 '{claim.status}'，无法 {action}", "not_claimed", http_status=409)
    if claim.claim_token != claim_token:
        raise PoolError("claim_token 不匹配", "token_mismatch", http_status=403)
    if claim.caller_id != caller_id or claim.task_id != task_id:
        raise PoolError("caller_id 或 task_id 与领取记录不一致", "caller_mismatch", http_status=403)
    if project and claim.project and project != claim.project:
        # 规格不变量 3 要求项目也要一致，但 OEP 的码表里没有 CLAIM_MISMATCH——
        # 用 caller_mismatch 报出去，注册机对 403 的处理路径不变，不会因为新码走错分支。
        raise PoolError("project_key 与领取记录不一致", "caller_mismatch", http_status=403)


def release_claim(
    db: Session,
    settings: Settings,
    *,
    account_id: int,
    claim_token: str,
    caller_id: str,
    task_id: str,
    project: str = "",
    reason: str = "",
    now: datetime | None = None,
) -> str:
    """释放领取：邮箱直接回 ``available``（OEP 语义：释放不计成功、不进冷却）。

    返回邮箱的**实际**池状态。OEP 这里硬编码回 ``"available"``；运营手工冻结过的邮箱
    不会被一次释放复活，那时如实回 ``frozen``/``retired``，不跟着 OEP 说假话。
    """

    current = now or utc_now()
    expire_stale_claims(db, settings, current)
    claim = load_claim(db, account_id)
    _validate_claim_ownership(
        claim, action="release", claim_token=claim_token, caller_id=caller_id, task_id=task_id, project=project
    )
    claim.status = CLAIM_RELEASED
    claim.completed_at = current
    claim.result_detail = reason or None

    mailbox = db.get(InventoryMailbox, claim.mailbox_id)
    status = claim.status
    if mailbox is not None:
        status = mailbox.pool_status
        if mailbox.pool_status == POOL_CLAIMED:
            mailbox.pool_status = POOL_AVAILABLE
            mailbox.pool_lease_expires_at = None
            mailbox.pool_cooldown_until = None
            status = POOL_AVAILABLE
    audit(
        db,
        event_type=EVENT_RELEASED,
        mailbox_id=claim.mailbox_id,
        details={"account_id": claim.id, "caller_id": caller_id, "task_id": task_id, "reason": reason},
    )
    db.commit()
    return status


def complete_claim(
    db: Session,
    settings: Settings,
    *,
    account_id: int,
    claim_token: str,
    caller_id: str,
    task_id: str,
    project: str = "",
    result: str,
    detail: str = "",
    now: datetime | None = None,
) -> str:
    """回传结果并推进池状态，返回邮箱的新池状态。

    结果 → 状态照 OEP 的 ``RESULT_TO_POOL_STATUS``：成功不再发、超时进冷却、
    被上游封禁进冻结、凭据失效进退休、网络错误立刻可再领。
    第二次回传（或释放后再回传）会命中 ``not_claimed``（409）——**效果只生效一次**。
    """

    current = now or utc_now()
    expire_stale_claims(db, settings, current)
    claim = load_claim(db, account_id)
    _validate_claim_ownership(
        claim, action="complete", claim_token=claim_token, caller_id=caller_id, task_id=task_id, project=project
    )

    new_status = RESULT_TO_POOL_STATUS[result]
    claim.status = CLAIM_COMPLETED
    claim.result = result
    claim.result_detail = detail or None
    claim.completed_at = current

    mailbox = db.get(InventoryMailbox, claim.mailbox_id)
    status = new_status
    if mailbox is not None:
        status = mailbox.pool_status
        if mailbox.pool_status == POOL_CLAIMED:
            # 运营手工冻结/退休过的邮箱不被流程状态覆盖：运营决定优先。
            mailbox.pool_status = new_status
            mailbox.pool_lease_expires_at = None
            mailbox.pool_cooldown_until = cooldown_deadline(current, settings) if new_status == POOL_COOLDOWN else None
            status = new_status
    audit(
        db,
        event_type=EVENT_COMPLETED,
        mailbox_id=claim.mailbox_id,
        details={
            "account_id": claim.id,
            "caller_id": caller_id,
            "task_id": task_id,
            "project": project,
            "result": result,
            "pool_status": status,
        },
    )
    db.commit()
    return status
