"""测试用的极简 IMAP 服务器（明文、内存）。

只实现本引擎用到的那几条命令：CAPABILITY / LOGIN / SELECT / STATUS / UID SEARCH /
UID FETCH / IDLE / LOGOUT。这样可以在没有真实 iCloud 账号的前提下验证线上的报文解析：
UID FETCH 的字面量配对、STATUS 的 UIDNEXT、IDLE 的 EXISTS 通知。
"""

import re
import socket
import threading
from datetime import UTC, datetime
from email.utils import format_datetime

CAPABILITY = "IMAP4rev1 IDLE"


class FakeImapServer:
    """把邮件放在内存里；`messages` 是 (uid, from, to, subject, body) 列表。"""

    def __init__(
        self,
        messages: list[tuple[int, str, str, str, str]] | None = None,
        *,
        password: str = "app-password",
        username: str = "user@example.com",
        accept_any_login: bool = False,
        idle_notify: bool = False,
        search_returns: list[int] | None = None,
        fixed_date: datetime | None = None,
    ) -> None:
        self.messages = messages or []
        self.password = password
        self.username = username
        self.accept_any_login = accept_any_login
        self.idle_notify = idle_notify
        self.search_returns = search_returns
        # 固定 INTERNALDATE/Date 头用于确定性断言；None = 当前时间（days 过滤可用）。
        self.fixed_date = fixed_date
        self.received: list[str] = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def address(self) -> str:
        host, port = self._server.getsockname()
        return f"{host}:{port}"

    def connect(self, _address: str):  # 供 ImapClient.connector 注入
        import imaplib

        client = imaplib.IMAP4("127.0.0.1", int(self.address.split(":")[1]))
        return client

    def start(self) -> "FakeImapServer":
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _serve(self) -> None:
        try:
            connection, _ = self._server.accept()
        except OSError:
            return
        with connection:
            handle = connection.makefile("rwb")
            handle.write(b"* OK [CAPABILITY " + CAPABILITY.encode() + b"] fake ready\r\n")
            handle.flush()
            try:
                self._session(connection, handle)
            except (OSError, ValueError):
                return

    def _session(self, connection: socket.socket, handle) -> None:
        while True:
            line = handle.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\r\n")
            self.received.append(text)
            tag, _, rest = text.partition(" ")
            command = rest.strip()
            upper = command.upper()
            if upper.startswith("CAPABILITY"):
                self._send(handle, f"* CAPABILITY {CAPABILITY}\r\n{tag} OK CAPABILITY completed\r\n")
            elif upper.startswith("LOGIN"):
                if self._login_ok(command):
                    self._send(handle, f"{tag} OK LOGIN completed\r\n")
                else:
                    self._send(handle, f"{tag} NO LOGIN failed\r\n")
            elif upper.startswith(("SELECT", "EXAMINE")):
                self._select(handle, tag, readonly=upper.startswith("EXAMINE"))
            elif upper.startswith("STATUS"):
                self._status(handle, tag)
            elif upper.startswith("UID SEARCH"):
                self._search(handle, tag, command)
            elif upper.startswith("UID FETCH"):
                self._fetch(handle, tag, command)
            elif upper.startswith("IDLE"):
                self._idle(connection, handle, tag)
            elif upper.startswith("LOGOUT"):
                self._send(handle, f"* BYE fake closing\r\n{tag} OK LOGOUT completed\r\n")
                return
            elif upper.startswith("NOOP"):
                self._send(handle, f"{tag} OK NOOP completed\r\n")
            else:
                self._send(handle, f"{tag} BAD unknown command\r\n")

    def _login_ok(self, command: str) -> bool:
        match = re.match(r'LOGIN\s+"?([^"\s]+)"?\s+"?([^"\s]+)"?', command, re.IGNORECASE)
        if not match:
            return False
        user, password = match.group(1), match.group(2)
        if self.accept_any_login:
            return True
        return user == self.username and password == self.password

    def _select(self, handle, tag: str, readonly: bool = True) -> None:
        access = "READ-ONLY" if readonly else "READ-WRITE"
        self._send(
            handle,
            f"* {len(self.messages)} EXISTS\r\n"
            "* FLAGS (\\Seen)\r\n"
            "* OK [UIDVALIDITY 1] UIDs valid\r\n"
            f"* OK [UIDNEXT {self._uid_next()}] Predicted next UID\r\n"
            f"{tag} OK [{access}] SELECT completed\r\n",
        )

    def _status(self, handle, tag: str) -> None:
        self._send(handle, f"* STATUS INBOX (UIDNEXT {self._uid_next()})\r\n{tag} OK STATUS completed\r\n")

    def _search(self, handle, tag: str, command: str) -> None:
        upper = command.upper()
        if "UID SEARCH ALL" in upper or upper.endswith("SEARCH"):
            uids = [uid for uid, *_ in self.messages]
        elif self.search_returns is not None:
            uids = list(self.search_returns)
        else:
            uids = [uid for uid, _, to, _, _ in self.messages if self._target(command) in to.lower()]
        joined = " ".join(str(uid) for uid in uids)
        self._send(handle, f"* SEARCH {joined}\r\n{tag} OK SEARCH completed\r\n")

    @staticmethod
    def _target(command: str) -> str:
        match = re.search(r'TO\s+"?([^"\s]+)"?', command, re.IGNORECASE)
        return match.group(1).lower() if match else ""

    def _fetch(self, handle, tag: str, command: str) -> None:
        sequence = re.search(r"UID FETCH (\S+)", command)
        wanted = self._expand(sequence.group(1) if sequence else "")
        for uid, sender, to, subject, body in self.messages:
            if uid not in wanted:
                continue
            raw = _raw_message(uid, sender, to, subject, body, self.fixed_date)
            # INTERNALDATE 默认当前时间：days>0 的过滤测试才能过（固定 2023 年会被滤掉）。
            internal = format_datetime(self.fixed_date or datetime.now(UTC))
            self._send(
                handle,
                f"* {uid} FETCH (UID {uid} INTERNALDATE \"{internal}\" RFC822 {{{len(raw)}}}\r\n".encode()
                + raw
                + b")\r\n",
            )
        self._send(handle, f"{tag} OK FETCH completed\r\n")

    def _expand(self, sequence: str) -> set[int]:
        if not sequence:
            return set()
        if ":" in sequence:
            first, last = sequence.split(":", 1)
            if first == "*" or last == "*":
                return {uid for uid, *_ in self.messages}
            return set(range(int(first), int(last) + 1))
        if sequence == "*":
            return {uid for uid, *_ in self.messages}
        return {int(part) for part in sequence.split(",") if part.isdigit()}

    def _idle(self, connection: socket.socket, handle, tag: str) -> None:
        self._send(handle, b"+ idling\r\n")
        if self.idle_notify:
            self._send(handle, f"* {len(self.messages) + 1} EXISTS\r\n")
        while True:
            line = handle.readline()
            if not line or line.strip().upper() == b"DONE":
                break
        self._send(handle, f"{tag} OK IDLE completed\r\n")

    def _uid_next(self) -> int:
        return max((uid for uid, *_ in self.messages), default=0) + 1

    @staticmethod
    def _send(handle, payload) -> None:
        data = payload.encode() if isinstance(payload, str) else payload
        handle.write(data)
        handle.flush()


def _raw_message(uid: int, sender: str, to: str, subject: str, body: str, fixed_date: datetime | None = None) -> bytes:
    date = format_datetime(fixed_date or datetime.now(UTC))
    return (
        f"From: {sender}\r\n"
        f"To: {to}\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: <msg-{uid}@example.com>\r\n"
        f"Date: {date}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()
