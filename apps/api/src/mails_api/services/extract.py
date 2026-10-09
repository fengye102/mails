"""Conservative context-bound numeric verification code extraction."""

import re

_CONTEXT = r"(?:\b(?:code|verification|verify)\b|验证码)"
_GAP = r"[^\d\n]{0,24}"
_DIGITS = r"(?<![0-9A-Za-z_])([0-9]{4,8})(?![0-9A-Za-z_])"
_PATTERNS = [
    re.compile(_CONTEXT + _GAP + _DIGITS, re.IGNORECASE),
    re.compile(_DIGITS + _GAP + _CONTEXT, re.IGNORECASE),
]


def extract_code(subject: str, body_text: str) -> str | None:
    candidates = {
        match.group(1)
        for text in (subject, body_text)
        for pattern in _PATTERNS
        for match in pattern.finditer(text)
    }
    return next(iter(candidates)) if len(candidates) == 1 else None
