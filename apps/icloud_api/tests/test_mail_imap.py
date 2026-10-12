"""IMAP 读信：报文解析（MIME/编码/日期）+ 假服务器上的真实报文往返。"""

from datetime import UTC, datetime, timedelta

import pytest
from fake_imap import FakeImapServer

from icloud_api.mail.imap_client import (
    ImapClient,
    ImapError,
    extract_bodies,
    parse_message,
)

ASCII_MESSAGE = (
    b"From: Sender <sender@example.com>\r\n"
    b"To: alias@icloud.com\r\n"
    b"Subject: Your code\r\n"
    b"Message-ID: <abc@example.com>\r\n"
    b"Date: Tue, 14 Nov 2023 22:13:20 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Your verification code is 739104.\r\n"
)

ENCODED_MESSAGE = (
    b"From: =?UTF-8?B?5byg5LiJ?= <zhang@example.com>\r\n"
    b"To: =?UTF-8?B?5Yir5ZCN?=@icloud.com\r\n"
    b"Subject: =?UTF-8?B?6aqM6K+B56CB?=\r\n"
    b"Date: Tue, 14 Nov 2023 22:13:20 +0800\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: quoted-printable\r\n"
    b"\r\n"
    b"=E9=AA=8C=E8=AF=81=E7=A0=81=EF=BC=9A112233\r\n"
)

MULTIPART_HTML_ONLY = (
    b"From: sender@example.com\r\n"
    b"To: alias@icloud.com\r\n"
    b"Subject: Activate\r\n"
    b"Date: Tue, 14 Nov 2023 22:13:20 +0000\r\n"
    b'Content-Type: multipart/alternative; boundary="b"\r\n'
    b"\r\n"
    b"--b\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: quoted-printable\r\n"
    b"\r\n"
    b"<html><body><p>Hello</p><br><a href=3D\"https://example.com/confirm\">Confirm</a>"
    b"<script>alert(1)</script></body></html>\r\n"
    b"--b--\r\n"
)

MULTIPART_WITH_PLAIN = (
    b"From: sender@example.com\r\n"
    b"To: alias@icloud.com\r\n"
    b"Subject: Both\r\n"
    b"Date: Tue, 14 Nov 2023 22:13:20 +0000\r\n"
    b'Content-Type: multipart/mixed; boundary="x"\r\n'
    b"\r\n"
    b"--x\r\n"
    b'Content-Type: multipart/alternative; boundary="y"\r\n'
    b"\r\n"
    b"--y\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"plain body 42\r\n"
    b"--y\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<p>html body</p>\r\n"
    b"--y--\r\n"
    b"--x\r\n"
    b"Content-Type: application/pdf\r\n"
    b'Content-Disposition: attachment; filename="a.pdf"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"JVBERi0xLjQK\r\n"
    b"--x--\r\n"
)


def test_parse_plain_message() -> None:
    message = parse_message(ASCII_MESSAGE, uid=7)
    assert message.id == "7"
    assert message.sender == "Sender <sender@example.com>"
    assert message.to == "alias@icloud.com"
    assert message.subject == "Your code"
    assert message.date == "2023-11-14T22:13:20Z"
    assert message.rfc_message_id == "<abc@example.com>"
    assert "739104" in message.body_text
    assert "739104" in message.snippet
    assert message.content_type == "text/plain"


def test_parse_decodes_rfc2047_headers_and_quoted_printable() -> None:
    message = parse_message(ENCODED_MESSAGE, uid=8)
    assert message.sender == "张三 <zhang@example.com>"
    assert message.subject == "验证码"
    assert "验证码：112233" in message.body_text
    # +08:00 的时间按 UTC 归一。
    assert message.date == "2023-11-14T14:13:20Z"


def test_html_only_message_becomes_readable_text_and_keeps_link() -> None:
    message = parse_message(MULTIPART_HTML_ONLY, uid=9)
    assert "Hello" in message.body_text
    # 激活链接必须保留，否则邮件废掉。
    assert "https://example.com/confirm" in message.body_text
    assert message.body_html
    assert "alert(1)" not in message.body_text  # script 丢弃


def test_multipart_prefers_plain_and_skips_attachments() -> None:
    message = parse_message(MULTIPART_WITH_PLAIN, uid=10)
    assert message.body_text.strip() == "plain body 42"
    assert "JVBERi0" not in message.body_text


def test_snippet_is_single_line_and_bounded() -> None:
    raw = ASCII_MESSAGE.replace(b"Your verification code is 739104.", b"line one\r\nline two\r\n" + b"x" * 900)
    message = parse_message(raw, uid=11)
    assert "\n" not in message.snippet
    assert len(message.snippet) <= 500


def test_message_without_date_falls_back_to_internal_date() -> None:
    raw = b"From: a@example.com\r\nSubject: no date\r\n\r\nbody\r\n"
    message = parse_message(raw, uid=12, internal_date="14-Nov-2023 22:13:20 +0000")
    assert message.date == "2023-11-14T22:13:20Z"


def test_malformed_message_degrades_without_raising() -> None:
    message = parse_message(b"\xff\xfe not a message at all", uid=13, internal_date="14-Nov-2023 22:13:20 +0000")
    assert message.id == "13"
    assert message.date == "2023-11-14T22:13:20Z"


def test_extract_bodies_returns_content_type() -> None:
    import email

    text, html, content_type = extract_bodies(email.message_from_bytes(MULTIPART_HTML_ONLY))
    assert "Hello" in text
    assert "Confirm" in html
    assert content_type == "multipart/alternative"


# ---- 假服务器 ----


@pytest.fixture
def server():
    messages = [
        (1, "old@example.com", "alias@icloud.com", "Old", "older mail"),
        (2, "mid@example.com", "other@icloud.com", "Middle", "middle mail"),
        (3, "new@example.com", "alias@icloud.com", "New", "newest mail 739104"),
    ]
    instance = FakeImapServer(messages, fixed_date=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)).start()
    yield instance
    instance.stop()


def connect(server: FakeImapServer, **overrides) -> ImapClient:
    client = ImapClient(
        host="127.0.0.1",
        port=0,
        username="user@example.com",
        app_password="app-password",
        connector=server.connect,
        **overrides,
    )
    client.connect()
    return client


def test_login_and_inbox_count(server) -> None:
    client = connect(server)
    try:
        assert client.inbox_count() == 3
        assert client.uid_next() == 4
    finally:
        client.disconnect()
    assert any("LOGIN" in line.upper() for line in server.received)


def test_login_failure_is_reported_without_leaking_password(server) -> None:
    client = ImapClient(
        host="127.0.0.1",
        port=0,
        username="user@example.com",
        app_password="wrong-password",
        connector=server.connect,
    )
    with pytest.raises(ImapError) as info:
        client.connect()
    assert info.value.code == "IMAP_LOGIN_FAILED"
    assert "wrong-password" not in info.value.message


def test_list_inbox_parses_literals_and_dates(server) -> None:
    client = connect(server)
    try:
        messages = client.list_inbox(limit=10)
    finally:
        client.disconnect()
    assert [message.id for message in messages] == ["1", "2", "3"]
    assert messages[2].subject == "New"
    assert messages[2].sender == "new@example.com"
    assert messages[2].date == "2023-11-14T22:13:20Z"
    assert "739104" in messages[2].body_text
    assert messages[1].to == "other@icloud.com"


def test_list_inbox_limit_keeps_newest(server) -> None:
    client = connect(server)
    try:
        messages = client.list_inbox(limit=2)
    finally:
        client.disconnect()
    assert [message.id for message in messages] == ["2", "3"]


def test_list_inbox_days_filter_excludes_old_mail(server) -> None:
    client = connect(server)
    now = datetime(2023, 11, 20, tzinfo=UTC)
    try:
        recent = client.list_inbox(limit=10, days=1, now=now)
        everything = client.list_inbox(limit=10, now=now)
    finally:
        client.disconnect()
    # 假服务器里的邮件都是 2023-11-14，距 now 已超过一天。
    assert recent == []
    assert len(everything) == 3


def test_find_by_recipient_uses_server_search(server) -> None:
    client = connect(server)
    try:
        found = client.find_by_recipient("alias@icloud.com")
    finally:
        client.disconnect()
    assert [message.id for message in found] == ["1", "3"]
    assert any("UID SEARCH" in line.upper() and "TO" in line.upper() for line in server.received)


def test_find_by_recipient_falls_back_to_local_filter() -> None:
    messages = [
        (1, "a@example.com", "alias@icloud.com", "One", "first"),
        (2, "b@example.com", "alias@icloud.com", "Two", "second"),
    ]
    instance = FakeImapServer(messages, search_returns=[]).start()
    client = connect(instance)
    try:
        found = client.find_by_recipient("alias@icloud.com")
    finally:
        client.disconnect()
        instance.stop()
    assert [message.id for message in found] == ["1", "2"]


def test_get_full_returns_body(server) -> None:
    client = connect(server)
    try:
        message = client.get_full(3)
    finally:
        client.disconnect()
    assert message.id == "3"
    assert "newest mail" in message.body_text


def test_get_full_missing_uid_raises(server) -> None:
    client = connect(server)
    try:
        with pytest.raises(ImapError) as info:
            client.get_full(99)
    finally:
        client.disconnect()
    assert info.value.code == "IMAP_MESSAGE_NOT_FOUND"


def test_uid_search_next_when_uidnext_unavailable(server) -> None:
    client = connect(server)
    try:
        assert client.uid_search_next() == 4
    finally:
        client.disconnect()


def test_operations_require_connection() -> None:
    client = ImapClient(host="127.0.0.1", port=0, username="u", app_password="p")
    with pytest.raises(ImapError) as info:
        client.list_inbox()
    assert info.value.code == "IMAP_NOT_CONNECTED"


def test_connect_requires_credentials() -> None:
    client = ImapClient(host="127.0.0.1", port=0, username="", app_password="")
    with pytest.raises(ImapError) as info:
        client.connect()
    assert info.value.code == "IMAP_CONFIG_INVALID"


def test_connect_failure_is_reported() -> None:
    client = ImapClient(host="127.0.0.1", port=1, username="u", app_password="p", timeout=0.5)
    with pytest.raises(ImapError) as info:
        client.connect()
    assert info.value.code == "IMAP_CONNECT_FAILED"


def test_idle_sees_exists_notification() -> None:
    instance = FakeImapServer([], idle_notify=True).start()
    client = connect(instance)
    try:
        tag = client.start_idle()
        assert client.wait_idle_activity(timeout=5) is True
        client.stop_idle(tag)
    finally:
        client.disconnect()
        instance.stop()


def test_idle_timeout_returns_false() -> None:
    instance = FakeImapServer([]).start()
    client = connect(instance)
    try:
        tag = client.start_idle()
        assert client.wait_idle_activity(timeout=0.2) is False
        client.stop_idle(tag)
    finally:
        client.disconnect()
        instance.stop()


def test_within_days_treats_missing_date_as_recent() -> None:
    from icloud_api.mail.imap_client import Message, _within_days

    assert _within_days(Message(date=""), 1, datetime.now(UTC)) is True
    fresh = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _within_days(Message(date=fresh), 1, datetime.now(UTC)) is True
    assert _within_days(Message(date=stale), 1, datetime.now(UTC)) is False
