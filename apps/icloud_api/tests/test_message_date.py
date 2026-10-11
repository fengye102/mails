"""邮件时间解析：时间戳量级、带时区、裸时间（按 UTC+8）与非法值。"""

from datetime import UTC, datetime, timedelta, timezone

from icloud_api.mail.date import (
    CHINA_STANDARD_TIME,
    format_rfc3339_nano,
    normalize_message_date,
    parse_message_date,
)

EPOCH_SECONDS = 1_760_000_000  # 2025-10-09T08:53:20Z


def test_epoch_magnitudes_are_detected_by_size() -> None:
    expected = datetime.fromtimestamp(EPOCH_SECONDS, UTC)
    for raw in (
        str(EPOCH_SECONDS),
        str(EPOCH_SECONDS * 1_000),
        str(EPOCH_SECONDS * 1_000_000),
        str(EPOCH_SECONDS * 1_000_000_000),
    ):
        parsed, ok = parse_message_date(raw)
        assert ok, raw
        assert parsed == expected, raw


def test_negative_and_fractional_epoch_values() -> None:
    # Go 的 ParseInt(s, 10, 64) 拒绝负数时间戳（-1<<63 之外的 int64 下限之外），
    # 负数不进时间戳分支，字符串也匹配不上任何布局，所以整段被拒。
    parsed, ok = parse_message_date("-1760000000")
    assert not ok and parsed is None

    parsed, ok = parse_message_date("1760000000500")
    assert ok and parsed == datetime.fromtimestamp(EPOCH_SECONDS, UTC) + timedelta(milliseconds=500)


def test_naive_times_are_read_as_china_standard_time() -> None:
    expected = datetime(2026, 10, 11, 23, 53, 3, tzinfo=CHINA_STANDARD_TIME)
    for raw in ("2026-10-11T23:53:03", "2026-10-11 23:53:03", "2026/10/11 23:53:03", "2026-10-11 23:53"):
        parsed, ok = parse_message_date(raw)
        assert ok, raw
        if raw.endswith("23:53"):
            assert parsed == expected.replace(second=0)
        else:
            assert parsed == expected


def test_naive_time_with_milliseconds_and_zone_abbreviations() -> None:
    parsed, ok = parse_message_date("2026-10-11T23:53:03.25")
    assert ok and parsed == datetime(2026, 10, 11, 23, 53, 3, 250_000, tzinfo=CHINA_STANDARD_TIME)

    parsed, ok = parse_message_date("2026-10-11 23:53:03 CST")
    assert ok and parsed.utcoffset() == timedelta(hours=8)

    parsed, ok = parse_message_date("2026-10-11 23:53:03 GMT")
    assert ok and parsed.utcoffset() == timedelta(0)


def test_mail_date_headers() -> None:
    parsed, ok = parse_message_date("Tue, 12 Oct 2026 00:01:02 +0800")
    assert ok and parsed == datetime(2026, 10, 12, 0, 1, 2, tzinfo=timezone(timedelta(hours=8)))

    parsed, ok = parse_message_date("Tue, 12 Oct 2026 00:01:02 GMT")
    assert ok and parsed == datetime(2026, 10, 12, 0, 1, 2, tzinfo=UTC)

    parsed, ok = parse_message_date("12 Oct 2026 00:01:02 +0800")
    assert ok and parsed == datetime(2026, 10, 12, 0, 1, 2, tzinfo=timezone(timedelta(hours=8)))

    parsed, ok = parse_message_date("12 Oct 26 00:01 GMT")
    assert ok and parsed == datetime(2026, 10, 12, 0, 1, tzinfo=UTC)

    parsed, ok = parse_message_date("Tuesday, 12-Oct-26 00:01:02 GMT")
    assert ok and parsed == datetime(2026, 10, 12, 0, 1, 2, tzinfo=UTC)


def test_iso_with_explicit_offset() -> None:
    # Go 的 ParseInLocation 对带显式偏移的串保留该偏移（不是把整段当成默认时区）。
    parsed, ok = parse_message_date("2026-10-11T23:53:03+08:00")
    assert ok
    assert parsed.astimezone(UTC) == datetime(2026, 10, 11, 15, 53, 3, tzinfo=UTC)

    parsed, ok = parse_message_date("2026-10-11T15:53:03.5Z")
    assert ok and parsed == datetime(2026, 10, 11, 15, 53, 3, 500_000, tzinfo=UTC)


def test_rejects_garbage_and_out_of_range_years() -> None:
    for raw in ("", "   ", "not a date", "1969-01-01 00:00:00"):
        parsed, ok = parse_message_date(raw)
        assert not ok and parsed is None, raw
    # 99999 在 int64 范围内，Go 会当成 1970-01-02 的秒数接受。
    parsed, ok = parse_message_date("99999")
    assert ok and parsed == datetime.fromtimestamp(99999, UTC)


def test_go_provider_shapes_match_the_original_suite() -> None:
    """CYMail TestParseMessageDateSupportsProviderTimestampShapes 的原样样本。"""
    want = datetime(2026, 8, 9, 4, 5, 6, tzinfo=UTC)
    for raw in (
        "1786248306",
        "1786248306000",
        "1786248306000000",
        "1786248306000000000",
        "2026-08-09T12:05:06+08:00",
        "2026-08-09T12:05:06",
        "2026-08-09 12:05:06",
        "2026-08-09 12:05:06 CST",
        "Sun, 09 Aug 2026 12:05:06 +0800",
    ):
        parsed, ok = parse_message_date(raw)
        assert ok, raw
        assert parsed.astimezone(UTC) == want, raw
    assert parse_message_date("not-a-date") == (None, False)
    # 纯数字但超出 int64：Go 的 ParseInt 失败，不当时间戳。
    assert parse_message_date("99999" + "0" * 20) == (None, False)


def test_normalize_returns_utc_rfc3339_or_the_original_text() -> None:
    assert normalize_message_date("2026-10-11 23:53:03") == "2026-10-11T15:53:03Z"
    assert normalize_message_date("2026-10-11 23:53:03.25") == "2026-10-11T15:53:03.25Z"
    assert normalize_message_date("  not a date  ") == "not a date"
    assert format_rfc3339_nano(datetime(2026, 10, 11, 15, 53, 3, tzinfo=UTC)) == "2026-10-11T15:53:03Z"
