"""``apps.mailboxes.sanitizer`` 单元测试（开发文档 §6.2）。

覆盖：XSS（script / on* 事件 / javascript: 与 data: 协议 / style 中的
expression()）、远程图片占位、cid: 内联图片、纯文本净化。
"""

import pytest

from apps.mailboxes.sanitizer import (
    ALLOWED_ATTRS,
    ALLOWED_TAGS,
    REMOTE_IMAGE_CLASS,
    REMOTE_IMAGE_PLACEHOLDER,
    sanitize_html,
    sanitize_text,
)


# ----------------------------------------------------------------- XSS 防护
class TestXsSProtection:
    """XSS 必须被清除（开发文档 §10.3 安全审查清单）。"""

    def test_script_tag_and_its_content_removed(self):
        out = sanitize_html('<p>安全内容</p><script>alert("xss")</script>')
        assert "<script" not in out.lower()
        assert "alert" not in out
        assert "安全内容" in out

    def test_script_with_src_removed(self):
        out = sanitize_html('<script src="http://evil.example/x.js"></script>')
        assert out == ""
        assert "evil.example" not in out

    def test_event_handler_attributes_removed(self):
        out = sanitize_html('<img src="http://e.example/a.png" onerror="alert(1)">')
        assert "onerror" not in out.lower()
        assert "alert(1)" not in out

    def test_onclick_removed_but_href_kept(self):
        out = sanitize_html('<a href="http://e.example" onclick="alert(1)">x</a>')
        assert "onclick" not in out.lower()
        assert 'href="http://e.example"' in out

    def test_javascript_protocol_removed(self):
        out = sanitize_html('<a href="javascript:alert(1)">click</a>')
        assert "javascript" not in out.lower()
        assert "href" not in out.lower()
        assert "click" in out

    def test_data_protocol_removed(self):
        out = sanitize_html('<img src="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=">')
        assert "data:image" not in out.lower()
        assert "base64" not in out.lower()

    def test_style_tag_content_removed(self):
        out = sanitize_html(
            "<style>body{background:url(javascript:alert(1))}</style><p>x</p>"
        )
        assert "<style" not in out.lower()
        assert "javascript" not in out.lower()
        assert "<p>x</p>" in out

    def test_nested_dangerous_tag_removed(self):
        out = sanitize_html('<div><iframe src="http://evil.example"></iframe></div>')
        assert "iframe" not in out.lower()
        assert "evil.example" not in out


class TestInlineStyleSanitizing:
    """style 中的 expression()/javascript: 必须被清除（§6.2 额外要求）。"""

    def test_css_expression_removed(self):
        out = sanitize_html('<div style="width: expression(alert(1)); color: red">x</div>')
        assert "expression" not in out.lower()
        assert "color: red" in out

    def test_css_javascript_url_removed(self):
        out = sanitize_html(
            '<span style="background-image: url(javascript:alert(1))">y</span>'
        )
        assert "javascript" not in out.lower()
        assert "url(" not in out.lower()
        assert out == "<span>y</span>"

    def test_css_behavior_removed(self):
        out = sanitize_html('<div style="behavior: url(#default#time2); color: blue">z</div>')
        assert "behavior" not in out.lower()
        assert "color: blue" in out

    def test_css_data_url_removed(self):
        out = sanitize_html(
            '<div style="background: url(data:text/html;base64,PHNjcmlwdD4=)">z</div>'
        )
        assert "data:" not in out.lower()

    def test_safe_styles_preserved(self):
        out = sanitize_html(
            '<div style="color: #333; text-align: center; font-size: 14px">x</div>'
        )
        assert "color: #333" in out
        assert "text-align: center" in out
        assert "font-size: 14px" in out

    def test_style_not_allowed_on_p(self):
        """§6.2 只允许 span/div 带 style。"""
        out = sanitize_html('<p style="color: red">x</p>')
        assert "style" not in out.lower()


# ----------------------------------------------------------------- 协议白名单
class TestProtocols:
    def test_http_https_mailto_kept(self):
        assert 'href="http://e.example"' in sanitize_html('<a href="http://e.example">1</a>')
        assert 'href="https://e.example"' in sanitize_html('<a href="https://e.example">2</a>')
        assert 'href="mailto:a@b.example"' in sanitize_html(
            '<a href="mailto:a@b.example">3</a>'
        )

    def test_relative_link_kept(self):
        assert 'href="/tickets/1"' in sanitize_html('<a href="/tickets/1">x</a>')

    def test_allowed_whitelist_matches_doc(self):
        for tag in ("p", "br", "b", "strong", "i", "em", "u", "a", "img", "ul", "ol",
                    "li", "blockquote", "table", "thead", "tbody", "tr", "td", "th",
                    "h1", "h2", "h3", "h4", "span", "div"):
            assert tag in ALLOWED_TAGS
        assert ALLOWED_ATTRS["a"] == ["href", "title"]
        assert "src" in ALLOWED_ATTRS["img"]


# --------------------------------------------------------------- 远程图片占位
class TestRemoteImagePlaceholder:
    """远程图片默认替换为占位，用户点击后再加载（开发文档 §6.2）。"""

    def test_http_remote_image_replaced(self):
        out = sanitize_html('<img src="http://cdn.example/track.png" alt="图">')
        assert f'src="{REMOTE_IMAGE_PLACEHOLDER}"' in out
        assert 'data-src="http://cdn.example/track.png"' in out
        assert f'class="{REMOTE_IMAGE_CLASS}"' in out
        assert 'data-remote="1"' in out

    def test_https_remote_image_replaced(self):
        out = sanitize_html('<img src="https://cdn.example/a.png">')
        assert f'src="{REMOTE_IMAGE_PLACEHOLDER}"' in out
        assert 'data-src="https://cdn.example/a.png"' in out

    def test_cid_inline_image_not_replaced(self):
        out = sanitize_html('<img src="cid:image001@example.com" alt="cid">')
        assert 'src="cid:image001@example.com"' in out
        assert REMOTE_IMAGE_PLACEHOLDER not in out
        assert "data-remote" not in out

    def test_relative_image_not_replaced(self):
        out = sanitize_html('<img src="/static/local.png">')
        assert 'src="/static/local.png"' in out
        assert REMOTE_IMAGE_PLACEHOLDER not in out

    def test_attacker_data_remote_and_class_ignored(self):
        out = sanitize_html(
            '<img src="http://cdn.example/a.png" data-remote="0" class="evil">'
        )
        assert 'data-remote="1"' in out
        assert 'class="evil"' not in out
        assert f'class="{REMOTE_IMAGE_CLASS}"' in out

    def test_forged_data_cid_javascript_dropped(self):
        out = sanitize_html('<img data-cid="javascript:alert(1)">')
        assert "javascript" not in out.lower()
        assert "data-cid" not in out.lower()


# --------------------------------------------------------------- 纯文本净化
class TestSanitizeText:
    def test_tags_stripped_and_entities_unescaped(self):
        assert sanitize_text("<p>a &amp; b</p>") == "a & b"

    def test_script_content_dropped(self):
        assert sanitize_text("<script>alert(1)</script>hello") == "hello"

    def test_plain_text_kept(self):
        assert sanitize_text("普通文本\n第二行") == "普通文本\n第二行"

    @pytest.mark.parametrize("value", ["", None])
    def test_empty_values(self, value):
        assert sanitize_text(value) == ""

    @pytest.mark.parametrize("value", ["", None])
    def test_sanitize_html_empty_values(self, value):
        assert sanitize_html(value) == ""
