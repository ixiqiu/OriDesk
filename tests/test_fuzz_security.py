"""对抗性输入测试：路径穿越、XSS、主题归正化的幂等性。

这组用例独立于各模块作者编写的测试，专门用"攻击者视角"的输入做回归。
"""

from __future__ import annotations

import pytest

from apps.core.utils import normalize_subject
from apps.mailboxes import storage
from apps.mailboxes.sanitizer import sanitize_html

TRAVERSAL_PAYLOADS = [
    "../../etc/passwd",
    "..\\..\\windows\\system32\\config\\sam",
    "....//....//etc/passwd",
    "/etc/passwd",
    "a/../../b",
    "./../../x",
    "nul\x00byte.txt",
    "con.txt",
    "a" * 400 + ".txt",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "..%2f..%2fetc",
    ".hidden",
    "..",
    ".",
    "",
]


@pytest.fixture
def media_root(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


@pytest.mark.parametrize("payload", TRAVERSAL_PAYLOADS)
def test_safe_component_never_escapes_media_root(media_root, payload):
    rel = storage.attachment_rel_path(7, "msg-1", payload)
    filename = rel.split("/")[-1]
    # 1) 文件名片段里不得出现任何分隔符（这正是穿越的前提）
    assert "/" not in filename
    assert "\\" not in filename
    assert "\x00" not in filename
    # 2) 路径中不得出现等于 ".." 或 "." 的片段（"a..b" 这类字面量无害）
    assert ".." not in rel.split("/")
    assert "." not in rel.split("/")
    # 3) 前缀必须是约定的 attachments/{ticket}/{message}/
    assert rel.startswith("attachments/7/msg-1/")
    # 4) 解析与写入都必须落在 MEDIA_ROOT 内
    absolute = storage.absolute_path(rel)
    assert media_root.resolve() in absolute.parents
    written = media_root / storage.save_file(rel, b"x")
    assert written.resolve().is_relative_to(media_root.resolve())


XSS_PAYLOADS = [
    '<script>alert(1)</script>',
    '<img src=x onerror="alert(1)">',
    '<a href="javascript:alert(1)">x</a>',
    '<a href="JaVaScRiPt:alert(1)">x</a>',
    '<iframe src="http://evil.example"></iframe>',
    '<svg/onload=alert(1)>',
    '<div style="background:url(javascript:alert(1))">x</div>',
    '<div style="width:expression(alert(1))">x</div>',
    '<body onload=alert(1)>',
    '<object data="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="></object>',
    '<math><mtext><table><mglyph><style><!--</style><img src=x onerror=alert(1)>',
    '<p onclick="alert(1)">click</p>',
    '<style>@import "http://evil.example/x.css";</style>',
    '<meta http-equiv="refresh" content="0;url=http://evil.example">',
    '<a href="data:text/html,<script>alert(1)</script>">x</a>',
    '<img src="file:///etc/passwd">',
]


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_sanitize_html_removes_executable_content(payload):
    cleaned = sanitize_html(payload).lower()
    for forbidden in (
        "<script",
        "</script",
        "onerror",
        "onload",
        "onclick",
        "javascript:",
        "expression(",
        "<iframe",
        "<object",
        "<embed",
        "<meta",
        "<style",
        "@import",
        "<svg",
        "<math",
        "file://",
        "data:text/html",
    ):
        assert forbidden not in cleaned, f"{payload!r} 净化后仍包含 {forbidden!r}：{cleaned!r}"


def test_markdown_like_text_is_inert():
    """纯文本里的 javascript: 不是 XSS（没有可执行属性），入库后由模板转义后原样显示。

    这里固定该行为，避免后续误把普通文本当漏洞处理。
    """
    cleaned = sanitize_html("[x](javascript:alert(1))")
    assert "javascript:" in cleaned
    # 关键不变量：净化结果里不存在带 javascript:/data: 协议的 href/src 属性
    assert "href=" not in cleaned
    assert "src=" not in cleaned


def test_sanitize_html_keeps_inline_cid_images():
    """cid: 内联图片不应被替换为远程占位（服务端可自行解析为附件）。"""
    cleaned = sanitize_html('<img src="cid:image001@example.com" alt="logo">')
    assert "cid:image001@example.com" in cleaned
    assert "remote-image" not in cleaned


def test_sanitize_html_rewrites_remote_images():
    cleaned = sanitize_html('<img src="http://tracker.example/pixel.gif">')
    assert "tracker.example/pixel.gif" in cleaned  # 原地址保留在 data-src
    assert 'data-remote="1"' in cleaned
    assert "remote-image-placeholder.svg" in cleaned


@pytest.mark.parametrize(
    "raw",
    [
        "Re: 主题",
        "Fwd: Re: 主题",
        "[T#12] 主题",
        "回复：转发: [T#3] 主题",
        "=?utf-8?b?5Li76aGY?=",
        "",
        "Re:",
        "[T#1]",
        "RE:FW:答复：[T#99] 正常主题（含括号）",
    ],
)
def test_normalize_subject_is_idempotent(raw):
    once = normalize_subject(raw)
    assert normalize_subject(once) == once
