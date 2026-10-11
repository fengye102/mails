"""iCloud 邮件 IMAP 客户端（对应 CYMail `internal/mail/client.go`，承自原 Python 版 icloud_mail.py）。

用 Apple 应用专用密码连 imap.mail.me.com:993，抓隐私邮箱别名收到的邮件。
只用标准库 `imaplib`（环境里没有第三方 IMAP 库），同步阻塞；上层异步路由通过线程池调用。

与原实现的**唯一有意偏差**：Go 的 `days` 过滤把 RFC3339 的时间戳拿去按 `time.RFC1123Z`
解析，永远解析失败，于是「只看近 N 天」实际从未生效。这里按本意实现该过滤
（解析本模块自己产出的 RFC3339 时间），并在测试里固定住行为。
"""

import email
import imaplib
import logging
import re
import select
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import Message as EmailMessage

from icloud_api.mail.htmltext import html_to_text

IMAP_SERVER = "imap.mail.me.com"
IMAP_PORT = 993
DEFAULT_LIST_LIMIT = 50
DEFAULT_RECIPIENT_LIMIT = 20
DEFAULT_TIMEOUT_SECONDS = 30
IDLE_CONTINUATION_TIMEOUT_SECONDS = 30

# 值得中断 IDLE 立即同步的未请求更新（EXISTS/RECENT/EXPUNGE/BYE）。
IDLE_ACTIVITY_PATTERN = re.compile(rb"\b(EXISTS|RECENT|EXPUNGE)\b")
IDLE_BYE_PATTERN = re.compile(rb"^\* BYE", re.IGNORECASE)
_UID_PATTERN = re.compile(rb"UID (\d+)")
_INTERNALDATE_PATTERN = re.compile(rb'INTERNALDATE "([^"]+)"')


class ImapError(Exception):
    """IMAP 失败。`code` 给调用方判类别，消息里不含凭据。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


@dataclass
class Message:
    """一封邮件的摘要（`body_text` 只有取详情时才填）。"""

    id: str = ""
    sender: str = ""
    to: str = ""
    subject: str = ""
    date: str = ""
    snippet: str = ""
    body_text: str = ""
    body_html: str = ""
    content_type: str = ""
    rfc_message_id: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class IdleOptions:
    """`start_uid` 是上次的增量游标（下一个待拉取的 UID），0 表示只收之后到达的新邮件。"""

    start_uid: int = 0
    idle_timeout: float = 20 * 60
    reconnect_max: float = 30.0
    # 单次 UID FETCH 覆盖的 UID 宽度：重连后避免超大回扫一次拉爆内存。
    fetch_chunk: int = 100
    # 外部游标首次同步的回扫安全余量（UID 空间，等价参考实现的 2 分钟重叠）。
    cursor_overlap: int = 100


@dataclass
class _FetchRecord:
    meta: bytes = b""
    raw: bytes = b""
    uid: int = 0
    internal_date: str = ""


def _decode_header(value: str) -> str:
    """解 RFC 2047 编码头（`=?UTF-8?B?...?=`）；解不开就原样返回。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - 畸形头不能拖垮整批邮件
        return value


def _decode_part(part: EmailMessage) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_bodies(message: EmailMessage) -> tuple[str, str, str]:
    """返回 (纯文本正文, HTML 原文, Content-Type)。优先 text/plain，其次由 HTML 转文本。"""
    content_type = message.get_content_type()
    plain: list[str] = []
    html: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        subtype = part.get_content_subtype()
        if subtype == "plain":
            plain.append(_decode_part(part))
        elif subtype == "html":
            html.append(_decode_part(part))
    text = "\n".join(chunk for chunk in plain if chunk).strip()
    html_text = "\n".join(chunk for chunk in html if chunk).strip()
    if not text and html_text:
        text = html_to_text(html_text)
    return text, html_text, content_type


def parse_message(raw: bytes, uid: int = 0, internal_date: str = "") -> Message:
    """把原始 RFC822 字节解析成 Message。"""
    try:
        parsed = email.message_from_bytes(raw)
    except Exception:  # noqa: BLE001 - 畸形邮件退化为空摘要，不能让整批同步失败
        return Message(id=str(uid), date=_normalize_date(internal_date))
    text, html_text, content_type = extract_bodies(parsed)
    message = Message(
        id=str(uid or _uid_from_headers(parsed) or ""),
        sender=_decode_header(parsed.get("From", "") or ""),
        to=_decode_header(parsed.get("To", "") or ""),
        subject=_decode_header(parsed.get("Subject", "") or ""),
        date=_message_date(parsed, internal_date),
        snippet=" ".join(text.split())[:500],
        body_text=text,
        body_html=html_text,
        content_type=content_type,
        rfc_message_id=(parsed.get("Message-ID", "") or "").strip(),
    )
    return message


def _uid_from_headers(parsed: EmailMessage) -> str:
    return ""


def _message_date(parsed: EmailMessage, internal_date: str) -> str:
    raw = (parsed.get("Date", "") or "").strip()
    parsed_date = _parse_header_date(raw)
    if parsed_date is not None:
        return parsed_date.strftime("%Y-%m-%dT%H:%M:%SZ")
    return _normalize_date(internal_date)


def _parse_header_date(raw: str) -> datetime | None:
    if not raw:
        return None
    from email.utils import parsedate_to_datetime

    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_date(raw: str) -> str:
    """把 IMAP 的 INTERNALDATE（`14-Nov-2023 22:13:20 +0000`）转成 RFC3339。"""
    if not raw:
        return ""
    for layout in ("%d-%b-%Y %H:%M:%S %z", "%d-%b-%Y %H:%M:%S"):
        try:
            value = datetime.strptime(raw, layout)
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def _within_days(message: Message, days: int, now: datetime | None = None) -> bool:
    if days <= 0:
        return True
    if not message.date:
        # 时间都取不到就别丢信：宁可多给一封，也不要漏掉验证码。
        return True
    moment = now or datetime.now(UTC)
    try:
        received = datetime.strptime(message.date, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return True
    return (moment - received).total_seconds() <= days * 86400


@dataclass
class ImapClient:
    """一个实例对应一个账号（或一个收件邮箱）的 IMAP 连接。

    `connector` 仅供测试注入（连明文假服务器）；为 None 时走 IMAP4_SSL。
    """

    host: str = IMAP_SERVER
    port: int = IMAP_PORT
    username: str = ""
    app_password: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    connector: object | None = None
    connection: imaplib.IMAP4 | None = field(default=None, repr=False)

    def connect(self) -> None:
        if not self.host or not self.username or not self.app_password:
            raise ImapError("IMAP_CONFIG_INVALID", "IMAP 主机、用户名和密码不能为空")
        address = f"{self.host}:{self.port}"
        try:
            if self.connector is not None:
                connection = self.connector(address)  # type: ignore[operator]
            else:
                connection = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        except (OSError, imaplib.IMAP4.error) as error:
            raise ImapError("IMAP_CONNECT_FAILED", f"IMAP 连接失败: {error}") from error
        try:
            connection.login(self.username, self.app_password)
        except (imaplib.IMAP4.error, OSError) as error:
            self._safe_logout(connection)
            raise ImapError(
                "IMAP_LOGIN_FAILED",
                f"IMAP 登录失败，请检查授权码或密码以及用户名 {self.username}",
            ) from error
        self.connection = connection

    def disconnect(self) -> None:
        if self.connection is not None:
            self._safe_logout(self.connection)
            self.connection = None

    @staticmethod
    def _safe_logout(connection: imaplib.IMAP4) -> None:
        try:
            connection.logout()
        except Exception:
            logging.getLogger(__name__).debug("IMAP 登出失败（忽略）", exc_info=True)

    def _require_connection(self) -> imaplib.IMAP4:
        if self.connection is None:
            raise ImapError("IMAP_NOT_CONNECTED", "未连接")
        return self.connection

    def select_inbox(self, readonly: bool = True) -> tuple[int, int]:
        """选中 INBOX，返回 (邮件数, UIDNEXT)。"""
        connection = self._require_connection()
        try:
            status, data = connection.select("INBOX", readonly=readonly)
        except (imaplib.IMAP4.error, OSError) as error:
            raise ImapError("IMAP_SELECT_FAILED", f"选择 INBOX 失败: {error}") from error
        if status != "OK":
            raise ImapError("IMAP_SELECT_FAILED", f"选择 INBOX 失败: {data}")
        exists = _first_int(data)
        return exists, self.uid_next()

    def uid_next(self) -> int:
        connection = self._require_connection()
        try:
            status, data = connection.status("INBOX", "(UIDNEXT)")
        except (imaplib.IMAP4.error, OSError) as error:
            raise ImapError("IMAP_STATUS_FAILED", f"读取 UIDNEXT 失败: {error}") from error
        if status != "OK":
            raise ImapError("IMAP_STATUS_FAILED", f"读取 UIDNEXT 失败: {data}")
        match = re.search(rb"UIDNEXT (\d+)", b" ".join(_as_bytes(item) for item in data))
        if match:
            return int(match.group(1))
        return self.uid_search_next()

    def inbox_count(self) -> int:
        connection = self._require_connection()
        try:
            status, data = connection.select("INBOX", readonly=False)
        except (imaplib.IMAP4.error, OSError) as error:
            raise ImapError("IMAP_SELECT_FAILED", f"选择 INBOX 失败: {error}") from error
        if status != "OK":
            raise ImapError("IMAP_SELECT_FAILED", f"选择 INBOX 失败: {data}")
        return _first_int(data)

    def _ensure_selected(self) -> None:
        """FETCH/SEARCH 只能在 SELECTED 状态执行；没选过就先只读选中 INBOX。"""
        connection = self._require_connection()
        if connection.state != "SELECTED":
            self.select_inbox(readonly=True)

    def uid_search_next(self) -> int:
        """UIDNEXT 取不到时用 UID SEARCH ALL 推导：最大 UID + 1（空邮箱为 1）。"""
        uids = self.uid_search_all()
        return (max(uids) + 1) if uids else 1

    def uid_search_all(self) -> list[int]:
        self._ensure_selected()
        connection = self._require_connection()
        try:
            status, data = connection.uid("SEARCH", None, "ALL")
        except (imaplib.IMAP4.error, OSError) as error:
            raise ImapError("IMAP_SEARCH_FAILED", f"UID SEARCH 失败: {error}") from error
        if status != "OK":
            return []
        return _parse_uid_list(data)

    def uid_search_recipient(self, recipient: str, days: int = 0) -> list[int]:
        self._ensure_selected()
        connection = self._require_connection()
        criteria: list[str] = ["TO", recipient]
        if days > 0:
            since = datetime.now(UTC).strftime("%d-%b-%Y")
            criteria = ["SINCE", since, "TO", recipient]
        try:
            status, data = connection.uid("SEARCH", None, *criteria)
        except (imaplib.IMAP4.error, OSError) as error:
            raise ImapError("IMAP_SEARCH_FAILED", f"UID SEARCH 失败: {error}") from error
        if status != "OK":
            return []
        return _parse_uid_list(data)

    def fetch_uid_range(self, first: int, last: int) -> list[Message]:
        return self._fetch(f"{first}:{last}")

    def fetch_uids(self, uids: list[int]) -> list[Message]:
        if not uids:
            return []
        return self._fetch(",".join(str(uid) for uid in uids))

    def _fetch(self, sequence: str) -> list[Message]:
        self._ensure_selected()
        connection = self._require_connection()
        try:
            status, data = connection.uid("FETCH", sequence, "(UID INTERNALDATE RFC822)")
        except (imaplib.IMAP4.error, OSError) as error:
            raise ImapError("IMAP_FETCH_FAILED", f"UID FETCH 失败: {error}") from error
        if status != "OK":
            raise ImapError("IMAP_FETCH_FAILED", f"UID FETCH 失败: {data}")
        return [parse_message(record.raw, record.uid, record.internal_date) for record in _group_fetch_data(data)]

    def list_inbox(self, limit: int = DEFAULT_LIST_LIMIT, days: int = 0, now: datetime | None = None) -> list[Message]:
        """按 UID 倒序取最近 limit 封（返回顺序与服务器一致，时间倒序）。"""
        self.select_inbox(readonly=True)
        count = limit if limit > 0 else DEFAULT_LIST_LIMIT
        uids = self.uid_search_all()
        if not uids:
            return []
        tail = uids[-count:]
        messages = self.fetch_uids(tail)
        return [message for message in messages if _within_days(message, days, now)]

    def find_by_recipient(
        self,
        recipient: str,
        limit: int = DEFAULT_RECIPIENT_LIMIT,
        days: int = 0,
        now: datetime | None = None,
    ) -> list[Message]:
        """找发给指定隐私邮箱别名的邮件：先服务端 TO 搜索，没命中再本地过滤。"""
        target = recipient.strip().lower()
        if not target:
            return []
        count = limit if limit > 0 else DEFAULT_RECIPIENT_LIMIT
        self.select_inbox(readonly=True)
        uids = self.uid_search_recipient(target, days)
        if uids:
            return self.fetch_uids(uids[-count:])
        # 服务端没命中（Apple 索引行为不稳定）：回退到「拉最近一批再本地过滤」。
        recent = self.list_inbox(count * 3, days, now)
        matched = [message for message in recent if target in message.to.lower()]
        return matched[:count]

    def get_full(self, uid: int, days: int = 0, now: datetime | None = None) -> Message:
        messages = self.fetch_uids([uid])
        if not messages:
            raise ImapError("IMAP_MESSAGE_NOT_FOUND", f"邮件不存在 (uid={uid})")
        _ = days, now
        return messages[0]

    # ---- IDLE 原语（供 mail/idle.py 编排）----

    def start_idle(self) -> bytes:
        connection = self._require_connection()
        try:
            tag = connection._new_tag()
            connection.send(tag + b" IDLE\r\n")
            line = connection.readline()
        except (imaplib.IMAP4.error, OSError) as error:
            raise ImapError("IMAP_IDLE_FAILED", f"进入 IDLE 失败: {error}") from error
        if not line.startswith(b"+"):
            raise ImapError("IMAP_IDLE_FAILED", f"服务器拒绝 IDLE: {line!r}")
        return tag

    def wait_idle_activity(self, timeout: float) -> bool:
        """等新邮件通知；返回 True 表示值得立即同步。超时返回 False，BYE/断线抛错。"""
        connection = self._require_connection()
        sock = connection.socket()
        readable, _, _ = select.select([sock], [], [], timeout)
        if not readable:
            return False
        try:
            line = connection.readline()
        except (imaplib.IMAP4.error, OSError) as error:
            raise ImapError("IMAP_IDLE_FAILED", f"IDLE 等待中断: {error}") from error
        if not line:
            raise ImapError("IMAP_IDLE_FAILED", "IDLE 连接已断开")
        if IDLE_BYE_PATTERN.match(line):
            raise ImapError("IMAP_IDLE_FAILED", "服务器关闭连接（BYE）")
        return bool(IDLE_ACTIVITY_PATTERN.search(line))

    def stop_idle(self, tag: bytes) -> None:
        connection = self._require_connection()
        try:
            connection.send(b"DONE\r\n")
            while True:
                line = connection.readline()
                if not line or line.startswith(tag):
                    break
        except (imaplib.IMAP4.error, OSError) as error:
            raise ImapError("IMAP_IDLE_FAILED", f"结束 IDLE 失败: {error}") from error


def _as_bytes(item: object) -> bytes:
    if isinstance(item, bytes):
        return item
    if isinstance(item, str):
        return item.encode()
    if isinstance(item, tuple) and item:
        return _as_bytes(item[0])
    return b""


def _first_int(data: list) -> int:
    for item in data or []:
        match = re.match(rb"^\s*(\d+)", _as_bytes(item))
        if match:
            return int(match.group(1))
    return 0


def _parse_uid_list(data: list) -> list[int]:
    joined = b" ".join(_as_bytes(item) for item in data)
    return [int(token) for token in re.findall(rb"\d+", joined)]


def _group_fetch_data(data: list) -> list[_FetchRecord]:
    """把 imaplib 的 FETCH 响应拼回「元信息 + 原始邮件」。

    响应形状是「元信息行、邮件字面量、（可选）结束括号」交替出现，不同服务器略有差别，
    所以按「元信息里带 UID、后面紧跟字面量」来配对，配不上的跳过而不是抛错。
    """
    records: list[_FetchRecord] = []
    pending: bytes | None = None
    for item in data or []:
        if isinstance(item, tuple):
            meta = _as_bytes(item[0])
            raw = item[1] if len(item) > 1 else b""
            records.append(_record(meta, raw if isinstance(raw, bytes) else _as_bytes(raw)))
            pending = None
            continue
        chunk = _as_bytes(item)
        if not chunk or chunk == b")":
            continue
        if _UID_PATTERN.search(chunk) and b"RFC822" in chunk.upper():
            pending = chunk
            continue
        if pending is not None:
            records.append(_record(pending, chunk))
            pending = None
    return records


def _record(meta: bytes, raw: bytes) -> _FetchRecord:
    uid_match = _UID_PATTERN.search(meta)
    date_match = _INTERNALDATE_PATTERN.search(meta)
    return _FetchRecord(
        meta=meta,
        raw=raw,
        uid=int(uid_match.group(1)) if uid_match else 0,
        internal_date=date_match.group(1).decode(errors="replace") if date_match else "",
    )
