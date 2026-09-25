"""§12.9 主题归正化验收用例。"""

from __future__ import annotations

from apps.core.utils import normalize_subject


def test_normalize_strips_re_prefix():
    assert normalize_subject("Re: 无法登录") == "无法登录"


def test_normalize_strips_fwd_prefix():
    assert normalize_subject("Fwd: 无法登录") == "无法登录"


def test_normalize_strips_chinese_prefix():
    assert normalize_subject("回复：无法登录") == "无法登录"
    assert normalize_subject("转发: 无法登录") == "无法登录"


def test_normalize_strips_ticket_no():
    assert normalize_subject("[T#123] 无法登录") == "无法登录"


def test_normalize_multiple_prefixes():
    assert normalize_subject("Re: Fwd: 回复：[T#45] 无法登录") == "无法登录"


def test_normalize_decodes_mime_header():
    encoded = "=?utf-8?b?5peg5rOV55m75b2V?="
    assert normalize_subject(f"Re: {encoded}") == "无法登录"
