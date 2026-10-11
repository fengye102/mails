"""把上游 HTML 正文收敛成纯文本（只用标准库）。

邮件正文常常是整页 HTML，而平台需要纯文本（验证码、行动链接都在文字里）。
超链接的 ``href`` 会以 ``文字 (URL)`` 的形式保留：激活类邮件只给链接，去掉目标等于废掉邮件；
``<script>``/``<style>``/图片等一律丢弃。转换是保守的：畸形 HTML 不允许抛错。

与 A/C 的同名模块是同一套语义的独立实现（引擎不导入 A/C 的包，见 Codex.md 的 D 阶段目录约定），
三边各有测试固定行为。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_SKIP_TAGS = {"head", "noscript", "script", "style", "template", "title"}
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_TAG_RE = re.compile(r"<[a-zA-Z!/][^>]*>")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._anchor_href = ""
        self._anchor_start = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "a":
            # 纯文本策略下链接文字会丢掉目标；激活邮件全靠这个 URL，必须保留。
            href = (dict(attrs).get("href") or "").strip()
            self._anchor_href = href if href.startswith(("http://", "https://")) else ""
            self._anchor_start = len(self.parts)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag == "a":
            href, self._anchor_href = self._anchor_href, ""
            visible = "".join(self.parts[self._anchor_start :]).strip()
            if href and visible != href:
                self.parts.append(f" ({href})")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _collapse(value: str) -> str:
    text = value.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    kept: list[str] = []
    for line in lines:
        if line:
            kept.append(line)
        elif kept and kept[-1] != "":
            kept.append("")
    return "\n".join(kept).strip()


def looks_like_html(value: str) -> bool:
    """只要出现标签形状就按 HTML 处理；普通文本里的 ``a < b`` 不算。"""
    return bool(value) and _TAG_RE.search(value) is not None


def html_to_text(value: str) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        text = _collapse("".join(parser.parts))
    except Exception:  # noqa: BLE001 - 畸形 HTML 只能退化为粗暴去标签
        text = ""
    if text:
        return text
    return _collapse(_TAG_RE.sub(" ", value))
