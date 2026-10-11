"""iCloud Hide My Email 别名解析与错误映射（对应 CYMail `internal/hme/client.go` 的纯函数部分）。

这里只放不发网络请求的部分：列表解析、创建时间、随机标签、容量/限速错误。
真正向上游发请求的客户端在账号接入后补（D4），解析规则必须先和原实现对齐。
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

MANAGED_CAPACITY_TARGET = 750
RANDOM_LABEL_LENGTH = 10
CLIENT_BUILD_NUMBER = "2624Build22"
CLIENT_MASTERING_NUMBER = "2624Build22"
DEFAULT_BUILD_NUMBER = "2624Build13"
REQUEST_TIMEOUT_SECONDS = 15
MAX_RETRIES = 3
RANDOM_LABEL_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
RETRY_DELAYS = (timedelta(seconds=1), timedelta(milliseconds=2500), timedelta(seconds=5))

_DATE_ONLY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_NAIVE_SECOND = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$")
_ISO_OFFSET = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})$"
)
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$")
_INT64_MAX = 2**63 - 1


class AliasError(Exception):
    """别名操作失败。`code` 是给调用方的稳定错误码，不带上游原文。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)


class AliasLimitReached(AliasError):
    def __init__(self, message: str = "") -> None:
        super().__init__("ALIAS_LIMIT_REACHED", message)


class AliasRateLimited(AliasError):
    def __init__(self, message: str = "", retry_after: timedelta = timedelta(0)) -> None:
        super().__init__("ALIAS_RATE_LIMITED", message)
        self.retry_after = retry_after if retry_after.total_seconds() > 0 else timedelta(0)


class TrustSessionRequired(AliasError):
    def __init__(self, message: str = "") -> None:
        super().__init__("TRUST_SESSION_REQUIRED", message)


def alias_retry_after(error: BaseException) -> timedelta:
    if isinstance(error, AliasRateLimited) and error.retry_after.total_seconds() > 0:
        return error.retry_after
    return timedelta(0)


@dataclass(frozen=True)
class Alias:
    email: str
    anonymous_id: str = ""
    label: str = ""
    active: bool = True
    created_at: str = ""
    forward_to_email: str = ""


@dataclass
class ForwardingSettings:
    aliases: list[Alias] = field(default_factory=list)
    selected_forward_to: str = ""
    forward_to_emails: list[str] = field(default_factory=list)


def new_random_label(existing: list[Alias]) -> str:
    """大小写不敏感地生成同时含字母和数字的 10 位标签，最多试 32 次。"""
    used = {alias.label.strip().lower() for alias in existing if alias.label.strip()}
    for _ in range(32):
        raw = os.urandom(RANDOM_LABEL_LENGTH)
        chars = [RANDOM_LABEL_ALPHABET[byte % len(RANDOM_LABEL_ALPHABET)] for byte in raw]
        if not any("a" <= char <= "z" for char in chars) or not any(char.isdigit() for char in chars):
            continue
        label = "".join(chars)
        if label not in used:
            return label
    raise AliasError("LABEL_EXHAUSTED", "could not generate a unique random label")


def parse_alias_created_at(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.isdigit() and int(raw) <= _INT64_MAX:
        number = int(raw)
        if number >= 1_000_000_000_000_000_000:
            seconds, nanos = divmod(number, 1_000_000_000)
            parsed = datetime.fromtimestamp(seconds, UTC) + timedelta(microseconds=nanos // 1000)
        elif number >= 1_000_000_000_000_000:
            parsed = datetime.fromtimestamp(number / 1_000_000, UTC)
        elif number >= 1_000_000_000_000:
            parsed = datetime.fromtimestamp(number / 1000, UTC)
        else:
            parsed = datetime.fromtimestamp(number, UTC)
        if 1970 <= parsed.year <= 3000:
            return parsed
        return None
    parsed = _parse_iso(raw) or _parse_naive(raw) or _parse_date(raw)
    return parsed.astimezone(UTC) if parsed else None


def normalize_alias_created_at(value: str) -> str:
    parsed = parse_alias_created_at(value)
    if parsed is None:
        return ""
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def operation_error(operation: str, body: str) -> AliasError:
    """把上游失败体映射成稳定错误。限速优先于容量，避免把临时封禁当成配额用尽。"""
    parsed = _loads(body)
    message = _first(
        _dig(parsed, "error", "errorMessage"),
        _dig(parsed, "error", "message"),
        _dig(parsed, "error", "reason"),
        parsed.get("message") if isinstance(parsed, dict) else "",
        "unknown error",
    )
    code = _first(
        _dig(parsed, "error", "errorCode"), _dig(parsed, "error", "code")
    ).lower()
    combined = f"{code} {message}".lower()
    retry_after = _parse_retry_after(parsed)
    rate_limited = retry_after.total_seconds() > 0 or any(
        token in combined
        for token in (
            "too many request", "rate limit", "rate_limit", "time limit", "time_limit",
            "throttl", "right now", "try again", "temporarily",
        )
    )
    if rate_limited:
        return AliasRateLimited(message, retry_after)
    # 只有明确的「地址总量」措辞才算容量用尽；"limit"/"too many" 同样出现在临时限速里。
    capacity = any(
        token in combined
        for token in (
            "maximum number of address", "maximum address", "address capacity",
            "total address", "capacity reached", "hide my email address limit",
            "limit of hide my email address",
        )
    )
    if capacity:
        return AliasLimitReached(message)
    return AliasError("ALIAS_OPERATION_FAILED", f"{operation} failed: {message}")


def parse_alias_list(body: str) -> list[Alias]:
    return parse_forwarding_settings(body).aliases


def parse_forwarding_settings(body: str) -> ForwardingSettings:
    settings = ForwardingSettings()
    root = _loads(body)
    if not isinstance(root, dict):
        return settings
    result = root.get("result") if isinstance(root.get("result"), dict) else {}
    settings.selected_forward_to = str(result.get("selectedForwardTo") or "").strip().lower()

    seen: set[str] = set()
    for item in result.get("forwardToEmails") or []:
        email = item if isinstance(item, str) else ""
        if isinstance(item, dict):
            email = _first(item.get("email"), item.get("emailAddress"), item.get("address"))
        email = str(email).strip().lower()
        if email and "@" in email and email not in seen:
            seen.add(email)
            settings.forward_to_emails.append(email)

    items = result.get("hmeEmails")
    if not isinstance(items, list):
        items = _find_first_dict_array(root)
    aliases: list[Alias] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metaData") or item.get("hmeMetadata") or item.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        email = _first(
            item.get("hme"), item.get("email"), item.get("alias"), item.get("address"), meta.get("hme")
        ).strip().lower()
        if not email or "@" not in email:
            continue
        state = _first(item.get("state"), item.get("status")).lower()
        active = state not in {"inactive", "deleted"}
        if "active" in item:
            active = bool(item.get("active")) and active
        if "isActive" in item:
            active = bool(item.get("isActive")) and active
        aliases.append(
            Alias(
                email=email,
                anonymous_id=_first(item.get("anonymousId"), item.get("id")),
                label=_first(item.get("label"), meta.get("label")),
                active=active,
                created_at=normalize_alias_created_at(
                    _first(
                        item.get("createTimestamp"), item.get("createdAt"), item.get("createdTimestamp"),
                        meta.get("createTimestamp"), meta.get("createdAt"), meta.get("createdTimestamp"),
                    )
                ),
                forward_to_email=_first(
                    item.get("forwardToEmail"), meta.get("forwardToEmail"), settings.selected_forward_to
                ).strip().lower(),
            )
        )
    aliases.sort(key=lambda alias: (not alias.active, alias.email))
    settings.aliases = aliases
    if settings.selected_forward_to and settings.selected_forward_to not in seen:
        settings.forward_to_emails.append(settings.selected_forward_to)
    return settings


def sync_alias_inventory(existing: list[Alias], upstream: list[Alias]) -> dict[str, list[Alias]]:
    """对照上游快照：新增、字段变化、上游已不存在的幽灵行。

    只按地址比对。幽灵行由调用方决定删还是标记，这里不直接动库，
    因为「有有效分配的别名」不能删（规格不变量 6）。
    """
    current = {alias.email: alias for alias in existing}
    seen = {alias.email: alias for alias in upstream}
    inserted = [alias for email, alias in seen.items() if email not in current]
    updated = [
        alias
        for email, alias in seen.items()
        if email in current and _alias_changed(current[email], alias)
    ]
    ghosts = [alias for email, alias in current.items() if email not in seen]
    return {"inserted": inserted, "updated": updated, "ghosts": ghosts}


def _alias_changed(left: Alias, right: Alias) -> bool:
    return (left.anonymous_id, left.label, left.active, left.forward_to_email) != (
        right.anonymous_id, right.label, right.active, right.forward_to_email,
    )


def _loads(body: str) -> object:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}


def _dig(value: object, *keys: str) -> str:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current).strip() if isinstance(current, str) else ""


def _first(*values: object) -> str:
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text:
            return text
    return ""


def _find_first_dict_array(value: object) -> list:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value
    if isinstance(value, dict):
        for child in value.values():
            found = _find_first_dict_array(child)
            if found:
                return found
    return []


def _parse_retry_after(parsed: object) -> timedelta:
    for path in (
        ("error", "retryAfter"), ("error", "retry_after"), ("error", "retryAfterSeconds"),
        ("retryAfter",), ("retry_after",), ("retryAfterSeconds",),
    ):
        raw = _lookup(parsed, path)
        if raw is None:
            continue
        text = str(raw).strip()
        try:
            seconds = float(text)
        except ValueError:
            seconds = 0
        if seconds > 0:
            return timedelta(seconds=seconds)
        match = _DURATION.fullmatch(text)
        if match and any(match.groups()):
            hours, minutes, secs = (float(part or 0) for part in match.groups())
            duration = timedelta(hours=hours, minutes=minutes, seconds=secs)
            if duration.total_seconds() > 0:
                return duration
        moment = _parse_iso(text)
        if moment is not None:
            remaining = moment - datetime.now(UTC)
            if remaining.total_seconds() > 0:
                return remaining
    return timedelta(0)


def _lookup(value: object, path: tuple[str, ...]) -> object:
    current = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _parse_iso(raw: str) -> datetime | None:
    match = _ISO_OFFSET.match(raw)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    digits = match[7] or ""
    micros = int(digits[:6].ljust(6, "0")) if digits else 0
    token = match[8]
    if token in {"Z", "z"}:
        zone = UTC
    else:
        sign = 1 if token[0] == "+" else -1
        compact = token[1:].replace(":", "")
        zone_offset = sign * timedelta(hours=int(compact[:2]), minutes=int(compact[2:4]))
        zone = UTC.__class__(zone_offset)
    try:
        return datetime(year, month, day, hour, minute, second, micros, tzinfo=zone)
    except ValueError:
        return None


def _parse_naive(raw: str) -> datetime | None:
    match = _NAIVE_SECOND.match(raw)
    if not match:
        return None
    parts = [int(part) for part in match.groups()]
    try:
        return datetime(*parts, tzinfo=UTC)
    except ValueError:
        return None


def _parse_date(raw: str) -> datetime | None:
    match = _DATE_ONLY.match(raw)
    if not match:
        return None
    parts = [int(part) for part in match.groups()]
    try:
        return datetime(*parts, tzinfo=UTC)
    except ValueError:
        return None
