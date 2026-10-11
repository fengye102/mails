"""C 侧 HTML→纯文本工具的单元测试。

取件响应有一条硬性不变量：只返回纯文本。上游（或未来的中枢推送）可能给整页 HTML，
所以入库时就必须转换；这里同时保证畸形 HTML 不抛错、不吞掉正文。
"""

from delivery_api.htmltext import html_to_text, looks_like_html


def test_plain_text_is_untouched():
    value = "Verification code: 123456"
    assert html_to_text(value) == value
    assert looks_like_html(value) is False


def test_tags_scripts_and_styles_are_dropped_and_entities_decoded():
    html = (
        "<!DOCTYPE html><html><head><title>t</title>"
        "<style>p{color:red}</style><script>alert(1)</script></head>"
        "<body><p>验证码：&nbsp;654321</p><div>hello&nbsp;world</div>"
        "<noscript>fallback</noscript></body></html>"
    )
    text = html_to_text(html)
    assert "654321" in text
    assert "hello world" in text
    assert "alert(1)" not in text
    assert "color:red" not in text
    assert "fallback" not in text
    assert "<" not in text and ">" not in text
    assert "&nbsp;" not in text


def test_block_tags_become_newlines_and_blank_runs_collapse():
    html = "<div>one</div>\n\n\n<div>   two   </div><br><br><p></p><p>three</p>"
    assert html_to_text(html) == "one\n\ntwo\n\nthree"


def test_malformed_html_still_yields_text():
    text = html_to_text("<p>a<b>b</p><div class=broken>c")
    assert "a" in text and "b" in text and "c" in text
    assert "<" not in text


def test_tagless_content_is_not_treated_as_html():
    assert looks_like_html("a < b > c") is False
    assert looks_like_html("<p>x</p>") is True
    assert html_to_text("") == ""


def test_anchor_targets_are_kept_as_plain_text():
    html = (
        '<p>激活 <a href="https://openxlab.org.cn/activate?token=abc">Activate</a></p>'
        '<p>已用链接 <a href="https://x.example/a">https://x.example/a</a></p>'
    )
    text = html_to_text(html)
    assert "Activate (https://openxlab.org.cn/activate?token=abc)" in text
    assert text.count("https://x.example/a") == 1
    assert looks_like_html(text) is False
    assert "<" not in text


def test_anchor_without_http_target_is_dropped():
    text = html_to_text('<a href="mailto:a@b.c">写信</a><a href="#top">回到顶部</a>')
    assert text == "写信回到顶部"
