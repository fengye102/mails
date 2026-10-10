import pytest
from mails_api.services.extract import extract_code


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
