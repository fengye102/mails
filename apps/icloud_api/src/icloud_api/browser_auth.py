"""浏览器授权（对应 CYMail `internal/server/browser_auth.go` 的 `icloud` 分支）。

流程：管理端发起 → 拿到一次性授权码（`iba_` + 32 字节随机数的 base64url，只存 SHA-256 摘要）
→ 用户在浏览器里登录 icloud.com，把 Cookie 回填到公开的 complete 接口 → 引擎用这份 Cookie
校验上游会话，成功才落库。

与原实现一致的关键点：
- 授权码 TTL 5 分钟，过期即失效，清理是惰性的（每次操作前先清一遍）；
- 校验上游之前先把状态从 `pending` 改成 `processing`，防止并发重复消费同一个授权码；
- 只有 `pending` 能消费；失败后状态为 `error` 且带 `last_error`，同码不可再用；
- 落库的是**校验后**的 Cookie（上游会通过 Set-Cookie 轮换 token，Go 保存的也是合并后的集合）；
- 163 收件箱网页授权（`kind=forwarding`）明确不移植。
"""

import base64
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from icloud_api.accounts import encrypt_json
from icloud_api.hme_client import AccountInfo, HmeClient
from icloud_api.models import ICloudAccount, ICloudBrowserAuthSession
from icloud_api.time_utils import as_utc

BROWSER_AUTH_TTL = timedelta(minutes=5)
AUTHORIZATION_CODE_PREFIX = "iba_"
MAX_COOKIES = 200
ACCOUNT_COOKIE_KIND = "icloud"

APPLE_ACCOUNT_PENDING_HINT = "Apple Account 管理会话引导尚未落地：浏览器授权只启用 iCloud 网页接口（约 5 个/小时）"
APPLE_ACCOUNT_ENABLED_HINT = "已启用 Apple Account 管理接口（约 20 个/小时）"
APPLE_ACCOUNT_DISABLED_HINT = (
    "未检测到 account.apple.com 会话：如需提高创建配额（约 25 个/小时），"
    "请在浏览器打开 https://account.apple.com 登录后重新授权"
)


class BrowserAuthError(Exception):
    """带稳定错误码的授权失败，路由层据此选 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _account_not_found() -> BrowserAuthError:
    return BrowserAuthError("ACCOUNT_NOT_FOUND", "账号不存在", 404)


def _invalid_code(message: str = "浏览器授权码无效或已过期") -> BrowserAuthError:
    return BrowserAuthError("INVALID_AUTHORIZATION_CODE", message, 401)


@dataclass(frozen=True)
class StartedBrowserAuth:
    request_id: str
    authorization_code: str
    expires_at: datetime


CookieValidator = Callable[[dict[str, str], ICloudAccount], Awaitable[tuple[dict[str, str], AccountInfo]]]
AppleAccountBootstrapper = Callable[[dict[str, str]], Awaitable[bool]]


async def validate_cookies_upstream(
    cookies: dict[str, str], account: ICloudAccount
) -> tuple[dict[str, str], AccountInfo]:
    """默认校验方式：拿提交上来的 Cookie 打一次上游 validate。"""
    async with HmeClient(
        cookies=dict(cookies), host=account.host or "icloud.com", proxy=account.proxy or ""
    ) as client:
        info = await client.validate_session()
        return dict(client.cookies), info


def purge_expired(db: Session, now: datetime | None = None) -> int:
    """惰性清理过期会话（对应 Go 的 cleanupBrowserAuthLocked）。

    过期判定放在 Python 里做：SQLite 存的是不带时区的时间戳，直接拿带时区的 now 去比
    会踩时区比较的坑（表里只有 5 分钟窗口内的会话，条数有界）。
    """
    moment = now or datetime.now(UTC)
    expired = [
        row
        for row in db.scalars(select(ICloudBrowserAuthSession)).all()
        if (as_utc(row.expires_at) or moment) <= moment
    ]
    for row in expired:
        db.delete(row)
    if expired:
        db.flush()
    return len(expired)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def start_browser_auth(
    db: Session,
    account: ICloudAccount,
    *,
    now: datetime | None = None,
    ttl: timedelta = BROWSER_AUTH_TTL,
) -> StartedBrowserAuth:
    moment = now or datetime.now(UTC)
    purge_expired(db, moment)
    if db.get(ICloudAccount, account.id) is None:
        raise BrowserAuthError("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    token = AUTHORIZATION_CODE_PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    row = ICloudBrowserAuthSession(
        account_id=account.id,
        kind=ACCOUNT_COOKIE_KIND,
        token_hash=_hash_token(token),
        status="pending",
        expires_at=moment + ttl,
    )
    db.add(row)
    db.flush()
    return StartedBrowserAuth(request_id=row.request_id, authorization_code=token, expires_at=row.expires_at)


def browser_auth_status(
    db: Session, account_id: str, request_id: str, *, now: datetime | None = None
) -> ICloudBrowserAuthSession:
    moment = now or datetime.now(UTC)
    purge_expired(db, moment)
    row = db.get(ICloudBrowserAuthSession, request_id)
    if row is None or row.account_id != account_id or row.kind != ACCOUNT_COOKIE_KIND:
        raise BrowserAuthError("REQUEST_NOT_FOUND", "浏览器授权请求不存在或已过期", 404)
    return row


async def complete_browser_auth(
    db: Session,
    *,
    authorization_code: str,
    cookies: dict[str, str],
    data_key: bytes,
    validate: CookieValidator = validate_cookies_upstream,
    bootstrap_apple_account: AppleAccountBootstrapper | None = None,
    now: datetime | None = None,
) -> dict:
    """回填 Cookie 完成授权。成功返回落库摘要，失败抛 BrowserAuthError。"""
    if not cookies or len(cookies) > MAX_COOKIES:
        raise BrowserAuthError("INVALID_COOKIES", "iCloud Cookie 数量无效", 400)
    moment = now or datetime.now(UTC)
    purge_expired(db, moment)

    row = db.scalars(
        select(ICloudBrowserAuthSession).where(
            ICloudBrowserAuthSession.token_hash == _hash_token(authorization_code)
        )
    ).one_or_none()
    if (
        row is None
        or row.kind != ACCOUNT_COOKIE_KIND
        or row.status != "pending"
        or (as_utc(row.expires_at) or moment) <= moment
    ):
        raise _invalid_code()

    account = db.get(ICloudAccount, row.account_id)
    if account is None:
        raise _account_not_found()

    # 先占位再校验上游：并发提交同一授权码时只有一个能走到这里。
    row.status = "processing"
    row.last_error = ""
    db.commit()

    try:
        merged_cookies, info = await validate(cookies, account)
    except Exception as error:  # 上游校验失败：如实记录，授权码作废
        row.status = "error"
        row.last_error = str(error)
        db.commit()
        raise BrowserAuthError(
            "COOKIE_VALIDATION_FAILED", "iCloud 会话校验失败，请在后台查看详情并重新授权", 400
        ) from error

    account.cookies_enc = encrypt_json(data_key, merged_cookies)
    account.status = "active"
    account.last_error = ""
    account.last_validated = moment
    if info.apple_id or info.primary_email:
        account.real_email = info.apple_id or info.primary_email
    if not account.icloud_email:
        account.icloud_email = derive_icloud_email(info)

    apple_account = await _try_bootstrap_apple_account(account, merged_cookies, bootstrap_apple_account)

    row.status = "completed"
    row.last_error = ""
    db.commit()

    return {
        "account_id": account.id,
        "cookies_count": len(merged_cookies),
        "apple_account": apple_account,
        "apple_account_supported": bootstrap_apple_account is not None,
        "apple_account_msg": _apple_account_hint(apple_account, bootstrap_apple_account is not None),
    }


async def _try_bootstrap_apple_account(
    account: ICloudAccount,
    cookies: dict[str, str],
    bootstrap: AppleAccountBootstrapper | None,
) -> bool:
    """引导 account.apple.com 会话提配额。没移植或失败都不阻塞本次授权（旧接口照常可用）。"""
    if bootstrap is None:
        return False
    try:
        return await bootstrap(cookies)
    except BrowserAuthError:
        raise
    except Exception:  # noqa: BLE001 - 引导失败不阻塞授权：旧接口照常可用，不向上抛上游细节
        return False


def _apple_account_hint(enabled: bool, supported: bool) -> str:
    if enabled:
        return APPLE_ACCOUNT_ENABLED_HINT
    if not supported:
        return APPLE_ACCOUNT_PENDING_HINT
    return APPLE_ACCOUNT_DISABLED_HINT


def derive_icloud_email(info: AccountInfo) -> str:
    """对应 Go 的 deriveICloudEmail：优先 iCloud 域名的地址，否则用 Apple ID 本地部分拼 icloud.com。"""
    primary = (info.primary_email or "").strip()
    apple_id = (info.apple_id or "").strip()
    if _is_icloud_domain(primary):
        return primary
    if _is_icloud_domain(apple_id):
        return apple_id
    if "@" in apple_id:
        return apple_id.split("@", 1)[0] + "@icloud.com"
    return primary or apple_id


def _is_icloud_domain(email: str) -> bool:
    return bool(email) and any(
        domain in email for domain in ("@icloud.com", "@me.com", "@mac.com")
    )
