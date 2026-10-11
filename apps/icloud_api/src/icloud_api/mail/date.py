"""邮件时间解析（对应 CYMail `internal/mail/date.go`）。

裸时间（没有时区）按中国标准时间（UTC+8）解释：原实现对 163 网页邮件的墙钟值就是这个选择，
iCloud 侧也可能出现裸时间，保持一致才能让同一封邮件两次入库的时间相等。

时区缩写按常见缩写表解析（CST=+8）；无法识别的缩写按原实现（Go `time.Parse`）退化为零偏移。
"""

import re
from datetime import UTC, datetime, timedelta, timezone

CHINA_STANDARD_TIME = timezone(timedelta(hours=8), "CST")

_EPOCH_NS = 1_000_000_000_000_000_000
_EPOCH_US = 1_000_000_000_000_000
_EPOCH_MS = 1_000_000_000_000

_NUMERIC = re.compile(r"^[+-]?\d+$")
_ISO_NAIVE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?$"
)
_ISO_OFFSET = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})$"
)
_DATE_SPACE_ZONE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}) ([A-Za-z]{2,5})$")
_DATE_SPACE_OFFSET = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})$")
_RFC1123 = re.compile(
    r"^\w{3}, (\d{1,2}) (\w{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2})(?: ([A-Za-z]{2,5}|[+-]\d{4}))?$"
)
_RFC822 = re.compile(
    r"^(\d{1,2}) (\w{3}) (\d{2,4}) (\d{2}):(\d{2})(?::(\d{2}))?(?: ([A-Za-z]{2,5}|[+-]\d{4}))?$"
)
_RFC850 = re.compile(r"^\w+, (\d{2})-(\w{3})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?: ([A-Za-z]{2,5}))?$")
_ANSIC = re.compile(r"^\w{3} (\w{3}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\d{4})$")
_NAIVE_MINUTE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2}) (\d{2}):(\d{2})$")
_NAIVE_SECOND = re.compile(
    r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?$"
)
# Go 的 ParseInt(s, 10, 64) 只接受完整的 int64；纯数字但溢出就不当时间戳。
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_ZONE_OFFSETS = {
    "UTC": 0, "GMT": 0, "UT": 0, "Z": 0,
    "CST": 8, "JST": 9, "KST": 9, "HKT": 8, "SGT": 8, "AEST": 10, "AEDT": 11,
    "PST": -8, "PDT": -7, "MST": -7, "MDT": -6, "CST6": -6, "EST": -5, "EDT": -4,
    "BST": 1, "CET": 1, "CEST": 2, "EET": 2, "MSK": 3,
}


def parse_message_date(value: str) -> tuple[datetime | None, bool]:
    raw = value.strip()
    if not raw:
        return None, False

    numeric = _parse_numeric(raw)
    if numeric is not None:
        return numeric, True

    for parser in (
        _iso_naive,
        _date_space_zone,
        _rfc1123,
        _iso_offset,
        _date_space_offset,
        _rfc822,
        _rfc850,
        _ansic,
        _naive_second,
        _naive_minute,
    ):
        parsed = parser(raw)
        if parsed is not None:
            return parsed, True

    return None, False


def normalize_message_date(value: str) -> str:
    parsed, ok = parse_message_date(value)
    if not ok or parsed is None:
        return value.strip()
    return format_rfc3339_nano(parsed)


def format_rfc3339_nano(value: datetime) -> str:
    """UTC 的 RFC3339，纳秒位按 Go 的做法去掉尾随零。"""
    utc_value = value.astimezone(UTC)
    base = utc_value.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f"{utc_value.microsecond:06d}".rstrip("0")
    return f"{base}.{fraction}Z" if fraction else f"{base}Z"


def _valid_year(value: datetime) -> bool:
    return 1970 <= value.astimezone(UTC).year <= 3000


def _parse_numeric(raw: str) -> datetime | None:
    if not _NUMERIC.match(raw):
        return None
    number = int(raw)
    if not _INT64_MIN <= number <= _INT64_MAX:
        return None
    magnitude = abs(number)
    if magnitude >= _EPOCH_NS:
        seconds, nanos = divmod(number, 1_000_000_000)
        parsed = datetime.fromtimestamp(seconds, UTC) + timedelta(microseconds=nanos // 1000)
    elif magnitude >= _EPOCH_US:
        seconds, micros = divmod(number, 1_000_000)
        parsed = datetime.fromtimestamp(seconds, UTC) + timedelta(microseconds=micros)
    elif magnitude >= _EPOCH_MS:
        seconds, millis = divmod(number, 1000)
        parsed = datetime.fromtimestamp(seconds, UTC) + timedelta(milliseconds=millis)
    else:
        parsed = datetime.fromtimestamp(number, UTC)
    return parsed if _valid_year(parsed) else None


def _fraction_to_microseconds(digits: str | None) -> int:
    if not digits:
        return 0
    return int(digits[:6].ljust(6, "0"))


def _zone_from_abbreviation(name: str) -> timezone:
    offset = _ZONE_OFFSETS.get(name.upper(), 0)
    return timezone(timedelta(hours=offset), name)


def _zone_from_offset(value: str) -> timezone:
    sign = 1 if value[0] == "+" else -1
    digits = value[1:].replace(":", "")
    hours = int(digits[:2])
    minutes = int(digits[2:4])
    # 时区名不要用 "+08:00"：datetime 会把它当成零偏移，astimezone 时小时数不变。
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _zone_from_token(token: str | None, default: timezone) -> timezone:
    if not token:
        return default
    if token in {"Z", "z"}:
        return UTC
    compact = token.replace(":", "")
    if compact[0] in "+-" and compact[1:].isdigit():
        return _zone_from_offset(compact)
    return _zone_from_abbreviation(token)


def _build(
    year: int, month: int, day: int, hour: int, minute: int, second: int, zone: timezone, micros: int
) -> datetime | None:
    try:
        parsed = datetime(year, month, day, hour, minute, second, micros, tzinfo=zone)
    except ValueError:
        return None
    return parsed if _valid_year(parsed) else None


def _iso_naive(raw: str) -> datetime | None:
    match = _ISO_NAIVE.match(raw)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    # Go 的 ParseInLocation 对带显式偏移的串仍保留该偏移，只把缺偏移的部分落到默认时区。
    zone = _zone_from_token(match[8], CHINA_STANDARD_TIME)
    return _build(
        year, month, day, hour, minute, second, zone, _fraction_to_microseconds(match[7])
    )


def _iso_offset(raw: str) -> datetime | None:
    match = _ISO_OFFSET.match(raw)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    return _build(
        year, month, day, hour, minute, second, _zone_from_token(match[8], UTC),
        _fraction_to_microseconds(match[7]),
    )


def _date_space_zone(raw: str) -> datetime | None:
    match = _DATE_SPACE_ZONE.match(raw)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    return _build(year, month, day, hour, minute, second, _zone_from_abbreviation(match[7]), 0)


def _date_space_offset(raw: str) -> datetime | None:
    match = _DATE_SPACE_OFFSET.match(raw)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    return _build(year, month, day, hour, minute, second, _zone_from_offset(match[7]), 0)


def _rfc1123(raw: str) -> datetime | None:
    match = _RFC1123.match(raw)
    if not match:
        return None
    day, month_name, year, hour, minute, second = match.groups()[:6]
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    return _build(
        int(year), month, int(day), int(hour), int(minute), int(second),
        _zone_from_token(match[7], CHINA_STANDARD_TIME), 0,
    )


def _rfc822(raw: str) -> datetime | None:
    match = _RFC822.match(raw)
    if not match:
        return None
    day, month_name, year, hour, minute, second, token = match.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    year_value = int(year)
    if len(year) == 2:
        year_value += 2000 if year_value < 70 else 1900
    return _build(
        year_value, month, int(day), int(hour), int(minute), int(second or 0),
        _zone_from_token(token, UTC), 0,
    )


def _rfc850(raw: str) -> datetime | None:
    match = _RFC850.match(raw)
    if not match:
        return None
    day, month_name, year, hour, minute, second, token = match.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    year_value = int(year)
    year_value += 2000 if year_value < 70 else 1900
    return _build(
        year_value, month, int(day), int(hour), int(minute), int(second),
        _zone_from_token(token, UTC), 0,
    )


def _ansic(raw: str) -> datetime | None:
    match = _ANSIC.match(raw)
    if not match:
        return None
    month_name, day, hour, minute, second, year = match.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    return _build(int(year), month, int(day), int(hour), int(minute), int(second), UTC, 0)


def _naive_second(raw: str) -> datetime | None:
    match = _NAIVE_SECOND.match(raw)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    return _build(
        year, month, day, hour, minute, second, CHINA_STANDARD_TIME,
        _fraction_to_microseconds(match[7]),
    )


def _naive_minute(raw: str) -> datetime | None:
    match = _NAIVE_MINUTE.match(raw)
    if not match:
        return None
    year, month, day, hour, minute = (int(part) for part in match.groups())
    return _build(year, month, day, hour, minute, 0, CHINA_STANDARD_TIME, 0)
