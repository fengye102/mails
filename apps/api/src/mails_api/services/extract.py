"""Conservative context-bound numeric verification code extraction."""

import re

_CONTEXT = r"(?:\b(?:(?:verification\s+)?codes?|verification|verify)\b|验证码)"
_DIGITS = r"(?<![0-9A-Za-z_])([0-9]{4,8})(?![0-9A-Za-z_])"
# Only common linking words/punctuation may separate a label and its value.
_FORWARD = re.compile(
    _CONTEXT + r"\s*(?:(?:is|are)\b|是|为)?\s*[:：=\-]?\s*" + _DIGITS,
    re.IGNORECASE,
)
_BACKWARD = re.compile(
    _DIGITS + r"\s+(?:(?:is|are)\s+)?(?:your\s+)?" + _CONTEXT,
    re.IGNORECASE,
)
_CLAUSES = re.compile(r"[;；.!?。！？\n\r]")
_NUMBERS = re.compile(_DIGITS)


def extract_code(subject: str, body_text: str) -> str | None:
    candidates = set()
    for text in (subject, body_text):
        for clause in _CLAUSES.split(text):
            if not (_FORWARD.search(clause) or _BACKWARD.search(clause)):
                continue
            # A label plus multiple distinct numeric values in one clause is
            # ambiguous, including "code: 123456 or 654321"; never pick the first.
            values = set(_NUMBERS.findall(clause))
            if len(values) != 1:
                return None
            candidates.update(values)
    return next(iter(candidates)) if len(candidates) == 1 else None
