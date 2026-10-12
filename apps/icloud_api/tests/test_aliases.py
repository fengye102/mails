"""别名解析纯函数测试（对照 CYMail `internal/hme/client.go` 的对应实现）。"""

import json
from datetime import timedelta

import pytest

from icloud_api.aliases import (
    Alias,
    AliasError,
    AliasLimitReached,
    AliasRateLimited,
    alias_retry_after,
    new_random_label,
    normalize_alias_created_at,
    operation_error,
    parse_alias_created_at,
    parse_alias_list,
    parse_forwarding_settings,
    sync_alias_inventory,
)


def _payload(**overrides: object) -> str:
    base = {
        "success": True,
        "result": {
            "selectedForwardTo": "Main@Example.com",
            "forwardToEmails": ["main@example.com", {"email": "Second@Example.com"}, "nope"],
            "hmeEmails": [
                {
                    "hme": "Alpha@icloud.com",
                    "anonymousId": "anon-alpha",
                    "label": "购物",
                    "state": "active",
                    "createTimestamp": "1700000000000",
                    "metaData": {},
                },
                {
                    "hme": "beta@icloud.com",
                    "id": "anon-beta",
                    "isActive": False,
                    "metaData": {"label": "论坛", "createTimestamp": "2023-11-14T22:13:20Z"},
                },
                {"notAnAlias": True},
            ],
        },
    }
    base["result"].update(overrides)  # type: ignore[union-attr]
    return json.dumps(base)


def test_parse_forwarding_settings_reads_aliases_and_forwarding() -> None:
    settings = parse_forwarding_settings(_payload())
    assert [alias.email for alias in settings.aliases] == ["alpha@icloud.com", "beta@icloud.com"]
    # 活跃的排前面，停下线的在后。
    assert settings.aliases[0].active is True
    assert settings.aliases[1].active is False
    assert settings.aliases[0].anonymous_id == "anon-alpha"
    assert settings.aliases[0].label == "购物"
    assert settings.aliases[1].label == "论坛"
    assert settings.aliases[0].created_at == "2023-11-14T22:13:20Z"
    # 默认转发地址按小写去重，且不合法的条目被丢掉。
    assert settings.forward_to_emails == ["main@example.com", "second@example.com"]
    assert settings.selected_forward_to == "main@example.com"
    # 别名没写转发地址时继承 Apple 的默认转发地址。
    assert settings.aliases[0].forward_to_email == "main@example.com"


def test_parse_forwarding_settings_sorts_active_then_email() -> None:
    body = json.dumps(
        {
            "result": {
                "hmeEmails": [
                    {"hme": "zulu@icloud.com", "state": "active"},
                    {"hme": "alpha@icloud.com", "state": "inactive"},
                    {"hme": "mike@icloud.com", "active": True},
                ]
            }
        }
    )
    settings = parse_forwarding_settings(body)
    assert [alias.email for alias in settings.aliases] == [
        "mike@icloud.com",
        "zulu@icloud.com",
        "alpha@icloud.com",
    ]


def test_parse_forwarding_settings_falls_back_to_first_object_array() -> None:
    body = json.dumps({"result": {"somethingElse": [{"hme": "nested@icloud.com", "status": "deleted"}]}})
    aliases = parse_alias_list(body)
    assert [alias.email for alias in aliases] == ["nested@icloud.com"]
    assert aliases[0].active is False


def test_parse_forwarding_settings_appends_selected_when_not_listed() -> None:
    body = json.dumps({"result": {"selectedForwardTo": "Only@Example.com", "hmeEmails": []}})
    settings = parse_forwarding_settings(body)
    assert settings.forward_to_emails == ["only@example.com"]


@pytest.mark.parametrize("body", ["", "not json", "[1, 2, 3]", "{}"])
def test_parse_forwarding_settings_tolerates_junk(body: str) -> None:
    settings = parse_forwarding_settings(body)
    assert settings.aliases == []
    assert settings.forward_to_emails == []


def test_active_flag_cannot_resurrect_deleted_alias() -> None:
    body = json.dumps({"result": {"hmeEmails": [{"hme": "x@icloud.com", "state": "deleted", "active": True}]}})
    assert parse_alias_list(body)[0].active is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1700000000", "2023-11-14T22:13:20Z"),
        ("1700000000000", "2023-11-14T22:13:20Z"),
        ("1700000000000000", "2023-11-14T22:13:20Z"),
        ("1700000000000000000", "2023-11-14T22:13:20Z"),
        ("2024-01-02T03:04:05Z", "2024-01-02T03:04:05Z"),
        ("2024-01-02T03:04:05.500Z", "2024-01-02T03:04:05Z"),
        ("2024-01-02T03:04:05+08:00", "2024-01-01T19:04:05Z"),
        ("2024-01-02 03:04:05", "2024-01-02T03:04:05Z"),
        ("2024-01-02", "2024-01-02T00:00:00Z"),
        ("  2024-01-02T03:04:05Z  ", "2024-01-02T03:04:05Z"),
        ("99999", "1970-01-02T03:46:39Z"),  # 年代久远的秒级时间戳仍算有效
    ],
)
def test_normalize_alias_created_at_shapes(raw: str, expected: str) -> None:
    assert normalize_alias_created_at(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not-a-date",
        "-1760000000",
        "99999999999999999999999",  # 超出 int64
        "2024-01-02 03:04:05Z",  # 带空格的偏移串不是 RFC3339，Go 也判失败
    ],
)
def test_normalize_alias_created_at_rejects(raw: str) -> None:
    assert normalize_alias_created_at(raw) == ""
    assert parse_alias_created_at(raw) is None


def test_new_random_label_shape_and_collision_avoidance(monkeypatch: pytest.MonkeyPatch) -> None:
    # alphabet 下标：0-25 是字母，26-35 是数字。
    alphabet_index = {char: index for index, char in enumerate("abcdefghijklmnopqrstuvwxyz0123456789")}
    label = "abcdef0123"
    sequences = [b"\x00" * 10, b"\x1a" * 10, bytes(alphabet_index[char] for char in label)]
    calls = iter(sequences)
    monkeypatch.setattr("icloud_api.aliases.os.urandom", lambda _size: next(calls))

    # 全字母、全数字各被拒一次，第三次才返回合法标签。
    assert new_random_label([]) == label

    calls = iter([bytes(alphabet_index[char] for char in label), bytes([25]) * 5 + bytes([35]) * 5])
    monkeypatch.setattr("icloud_api.aliases.os.urandom", lambda _size: next(calls))
    taken = [Alias(email="a@icloud.com", label=label.upper())]
    assert new_random_label(taken) == "zzzzz99999"


def test_new_random_label_gives_up_after_32_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    label = "abcdef0123"
    alphabet_index = {char: index for index, char in enumerate("abcdefghijklmnopqrstuvwxyz0123456789")}
    monkeypatch.setattr(
        "icloud_api.aliases.os.urandom",
        lambda _size: bytes(alphabet_index[char] for char in label),
    )
    with pytest.raises(AliasError) as info:
        new_random_label([Alias(email="a@icloud.com", label=label)])
    assert info.value.code == "LABEL_EXHAUSTED"


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"success": False, "error": {"errorCode": "too many requests", "errorMessage": "slow down"}}),
        json.dumps({"success": False, "error": {"errorMessage": "Please try again later"}}),
        json.dumps({"success": False, "error": {"errorMessage": "throttled"}}),
    ],
)
def test_operation_error_detects_rate_limit(body: str) -> None:
    error = operation_error("generate", body)
    assert isinstance(error, AliasRateLimited)
    assert error.code == "ALIAS_RATE_LIMITED"


def test_operation_error_rate_limit_keeps_retry_hint() -> None:
    body = json.dumps({"success": False, "error": {"errorMessage": "rate limit", "retryAfter": 90}})
    error = operation_error("generate", body)
    assert isinstance(error, AliasRateLimited)
    assert error.retry_after == timedelta(seconds=90)
    assert alias_retry_after(error) == timedelta(seconds=90)


@pytest.mark.parametrize(
    "message",
    [
        "You have reached the maximum number of addresses",
        "hide my email address limit reached",
        "Address capacity reached",
    ],
)
def test_operation_error_detects_capacity(message: str) -> None:
    body = json.dumps({"success": False, "error": {"errorMessage": message}})
    error = operation_error("reserve", body)
    assert isinstance(error, AliasLimitReached)
    assert error.code == "ALIAS_LIMIT_REACHED"


def test_operation_error_plain_failure_keeps_operation_name() -> None:
    body = json.dumps({"success": False, "error": {"errorMessage": "bad label"}})
    error = operation_error("reserve", body)
    assert error.code == "ALIAS_OPERATION_FAILED"
    assert "reserve failed" in error.message


def test_operation_error_unknown_body() -> None:
    error = operation_error("delete", "<html>oops</html>")
    assert "unknown error" in error.message
    assert alias_retry_after(error) == timedelta(0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("30", 30), ("2.5", 2.5), ("1h2m3s", 3723), ("45s", 45), ("bogus", 0), ("-5", 0), ("", 0)],
)
def test_parse_retry_after_shapes(raw: str, expected: float) -> None:
    body = json.dumps({"error": {"retryAfter": raw}})
    error = operation_error("generate", body)
    # 只要 retryAfter 解析出正值就会走限速分支。
    if expected:
        assert isinstance(error, AliasRateLimited)
        assert error.retry_after.total_seconds() == pytest.approx(expected)
    else:
        assert not isinstance(error, AliasRateLimited)


def test_parse_retry_after_absolute_timestamp() -> None:
    body = json.dumps({"error": {"retryAfter": "2999-01-01T00:00:00Z"}, "errorCode": "time limit"})
    error = operation_error("generate", body)
    assert isinstance(error, AliasRateLimited)
    assert error.retry_after.total_seconds() > 0


def test_sync_alias_inventory_classifies_rows() -> None:
    existing = [
        Alias(email="keep@icloud.com", anonymous_id="a1", label="one", active=True),
        Alias(email="stale@icloud.com", anonymous_id="a2", label="two", active=True),
        Alias(email="changed@icloud.com", anonymous_id="a3", label="three", active=True),
    ]
    upstream = [
        Alias(email="keep@icloud.com", anonymous_id="a1", label="one", active=True),
        Alias(email="changed@icloud.com", anonymous_id="a3", label="three", active=False),
        Alias(email="fresh@icloud.com", anonymous_id="a4", label="four", active=True),
    ]
    delta = sync_alias_inventory(existing, upstream)
    assert [alias.email for alias in delta["inserted"]] == ["fresh@icloud.com"]
    assert [alias.email for alias in delta["updated"]] == ["changed@icloud.com"]
    assert [alias.email for alias in delta["ghosts"]] == ["stale@icloud.com"]
