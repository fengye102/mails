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

_URL = re.compile(r"""https?://[^\s<>"'　()（）【】]+""")


def extract_link(subject: str, body_text: str) -> str | None:
    """取正文（其次主题）里第一个 http(s) 链接。

    纯文本阅读策略下这是用户唯一能点/能复制的行动线索（激活、重置密码等）；
    链接末尾的分隔标点与 HTML 转换补上的括号会被裁掉。
    """
    for text in (body_text, subject):
        match = _URL.search(text or "")
        if match is not None:
            return match.group(0).rstrip(".,;:!?)]}'\"”、。，；：！？）】")
    return None
