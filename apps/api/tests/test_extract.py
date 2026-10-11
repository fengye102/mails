import pytest
from mails_api.services.extract import extract_code, extract_link


@pytest.mark.parametrize(
    "subject,body,expected",
    [
        ("Your code", "Verification code: 123456", "123456"),
        ("Invoice 123456", "Order 654321 was paid", None),
        ("Code 123456", "Code 654321", None),
        ("验证码：1234", "", "1234"),
        ("", "123456 is your verification code", "123456"),
        ("Code 123456", "Code 123456", "123456"),
        ("Code 123456789", "", None),
        ("Decode 123456", "", None),
    ],
)
def test_extract_code(subject, body, expected):
    assert extract_code(subject, body) == expected


@pytest.mark.parametrize(
    "subject,body,expected",
    [
        ("验证码123456", "", "123456"),
        ("", "验证码是123456", "123456"),
    ],
)
def test_chinese_context_without_ascii_separator(subject, body, expected):
    assert extract_code(subject, body) == expected


@pytest.mark.parametrize(
    "subject,body",
    [
        ("Code: 123456 or 654321", ""),
        ("Verification codes: 123456 or 654321", ""),
        ("Verification failed; invoice 123456", ""),
        ("Verification failed. Invoice 123456", ""),
        ("Verification failed", "invoice 123456"),
    ],
)
def test_conflicts_and_unrelated_clauses_are_not_codes(subject, body):
    assert extract_code(subject, body) is None


def test_extract_link_prefers_body_and_trims_trailing_punctuation():
    body = "请点击激活 Activate (https://openxlab.org.cn/mails/activate?token=abc123)."
    assert (
        extract_link("注册激活", body)
        == "https://openxlab.org.cn/mails/activate?token=abc123"
    )


def test_extract_link_falls_back_to_subject_and_ignores_non_http():
    assert extract_link("http://example.com/reset", "无链接正文") == (
        "http://example.com/reset"
    )
    assert extract_link("账单", "没有链接，也没有 ftp://example.com") is None
    assert extract_link("", "") is None
