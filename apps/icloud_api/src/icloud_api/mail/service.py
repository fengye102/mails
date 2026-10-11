"""收信服务层：账号 → IMAP 客户端 → 读信（对应 CYMail `account/manager.go` 的 MailClient 预检）。

不变量（沿袭 CYMail）：
- IMAP 登录邮箱取 `icloud_email`，为空回退 `real_email`；必须是 iCloud 域名
  （@icloud.com / @me.com / @mac.com），否则拒绝连接；
- App Password 未设置直接报错，不做静默降级；
- 读信是只读操作：连接用只读 SELECT，读完即断开，不缓存连接。
"""

import logging
import re
from collections.abc import Callable

from sqlalchemy.orm import Session

from icloud_api.accounts import account_app_password
from icloud_api.mail.imap_client import ImapClient, ImapError
from icloud_api.models import ICloudAccount

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993
ICLOUD_DOMAIN_PATTERN = re.compile(r"@(icloud\.com|me\.com|mac\.com)$", re.IGNORECASE)

# 与 CYMail server.go 的 maxInboxLimit / maxInboxDays 对齐。
MAX_LIST_LIMIT = 200
MAX_DAYS = 365
DEFAULT_LIST_LIMIT = 20
DEFAULT_DAYS = 7


class MailServiceError(Exception):
    """收信预检失败。`code` 给路由映射 HTTP 状态。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def imap_login_email(account: ICloudAccount) -> str:
    """IMAP 登录邮箱：icloud_email 优先，回退 real_email（CYMail MailClient 语义）。"""
    email = (account.icloud_email or "").strip() or (account.real_email or "").strip()
    if not email:
        raise MailServiceError("MAIL_NO_EMAIL", "账号未设置 iCloud 邮箱")
    if not ICLOUD_DOMAIN_PATTERN.search(email):
        raise MailServiceError("MAIL_NOT_ICLOUD_EMAIL", f"账号未设置 iCloud 邮箱 (当前: {email})")
    return email


def build_imap_client(account: ICloudAccount, key: bytes, **overrides) -> ImapClient:
    """按账号凭据构造 IMAP 客户端（不连接）。overrides 供测试注入假连接器。"""
    username = imap_login_email(account)
    app_password = account_app_password(account, key)
    if not app_password:
        raise MailServiceError("MAIL_NO_APP_PASSWORD", "账号未设置 App 专用密码")
    options: dict = {
        "host": IMAP_HOST,
        "port": IMAP_PORT,
        "username": username,
        "app_password": app_password,
    }
    options.update(overrides)
    return ImapClient(**options)


def _bounded(value: int | None, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    return max(minimum, min(maximum, value))


def list_account_mail(
    db: Session,
    account: ICloudAccount,
    key: bytes,
    *,
    address: str = "",
    limit: int | None = None,
    days: int | None = None,
    client_factory: Callable[..., ImapClient] = build_imap_client,
) -> dict:
    """读信：传 address 只看发给该别名的邮件，否则返回收件箱最近邮件。

    阻塞调用（imaplib 是同步的）；FastAPI 路由用 `run_in_threadpool` 包装。
    返回形状对齐 CYMail `GET /api/inbox`：count/messages/method。
    """
    bounded_limit = _bounded(limit, DEFAULT_LIST_LIMIT, 1, MAX_LIST_LIMIT)
    bounded_days = _bounded(days, DEFAULT_DAYS, 0, MAX_DAYS)
    target = (address or "").strip().lower()
    client = client_factory(account, key)
    try:
        client.connect()
        if target:
            messages = client.find_by_recipient(target, bounded_limit, bounded_days)
            method = "imap_recipient_search"
        else:
            messages = client.list_inbox(bounded_limit, bounded_days)
            method = "imap"
    except ImapError as error:
        raise MailServiceError(error.code, error.message) from error
    finally:
        try:
            client.disconnect()
        except Exception:
            logging.getLogger(__name__).debug("IMAP 断开失败（忽略）", exc_info=True)
    return {
        "account_id": account.id,
        "address": target,
        "count": len(messages),
        "messages": [message.as_dict() for message in messages],
        "method": method,
    }
