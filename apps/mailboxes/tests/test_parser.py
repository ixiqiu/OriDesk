"""``apps.mailboxes.parser`` 单元测试（开发文档 §6.3 / §6.5）。

全部用自造 raw MIME 字节串构造邮件，覆盖 RFC2047 中文主题、纯文本、纯 HTML、
multipart/alternative、嵌套 multipart/mixed 带附件、base64 附件、超限附件跳过、
iso-8859-1 与未知编码回退、message/rfc822 嵌套。
"""

import base64
import logging

import pytest
from django.core.cache import cache

from apps.audit.models import Setting
from apps.mailboxes.parser import (
    extract_attachments,
    extract_body,
    extract_headers,
    parse_inbound,
    parse_mime,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_setting_cache():
    """Setting.get 走 Django cache，测试间必须清干净。"""
    cache.clear()
    yield
    cache.clear()


def _b64_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _b64_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ------------------------------------------------------------------- 头字段
class TestExtractHeaders:
    def test_rfc2047_chinese_subject_decoded(self):
        raw = (
            b"From: Zhang San <zhangsan@example.com>\r\n"
            b"To: support@example.com\r\n"
            b"Subject: =?utf-8?B?" + _b64_text("中文主题测试").encode("ascii") + b"?=\r\n"
            b"Message-ID: <msg-1@example.com>\r\n"
            b"Date: Mon, 01 Jan 2024 10:00:00 +0800\r\n"
            b"\r\n"
            b"body\r\n"
        )
        headers = extract_headers(parse_mime(raw))
        assert headers["subject"] == "中文主题测试"
        assert headers["from"] == "Zhang San <zhangsan@example.com>"
        assert headers["message_id"] == "<msg-1@example.com>"
        assert headers["date"] == "Mon, 01 Jan 2024 10:00:00 +0800"

    def test_rfc2047_chinese_display_name_decoded(self):
        raw = (
            b"From: =?utf-8?B?" + _b64_text("张三").encode("ascii")
            + b"?= <zhangsan@example.com>\r\n"
            b"To: support@example.com\r\n"
            b"Subject: hi\r\n"
            b"\r\n"
            b"body\r\n"
        )
        data = parse_inbound(raw)
        assert data["headers"]["from"] == "张三 <zhangsan@example.com>"
        assert data["from_addr"] == "zhangsan@example.com"

    def test_raw_utf8_header_without_rfc2047_still_readable(self):
        """部分客户端直接发原始 UTF-8 头，不应变成乱码。"""
        raw = "Subject: 测试主题\r\nFrom: 张三 <a@b.com>\r\n\r\nbody\r\n".encode("utf-8")
        headers = extract_headers(parse_mime(raw))
        assert headers["subject"] == "测试主题"
        assert "张三" in headers["from"]

    def test_all_keys_exist_and_are_str(self):
        headers = extract_headers(parse_mime(b"From: a@b.com\r\n\r\nbody\r\n"))
        assert set(headers) == {
            "message_id", "in_reply_to", "references", "from",
            "to", "cc", "subject", "date",
        }
        assert all(isinstance(value, str) for value in headers.values())
        assert headers["subject"] == ""
        assert headers["cc"] == ""

    def test_in_reply_to_and_references(self):
        raw = (
            b"From: a@b.com\r\n"
            b"Message-ID: <m2@b.com>\r\n"
            b"In-Reply-To: <m1@b.com>\r\n"
            b"References: <m0@b.com> <m1@b.com>\r\n"
            b"\r\n"
            b"body\r\n"
        )
        headers = extract_headers(parse_mime(raw))
        assert headers["in_reply_to"] == "<m1@b.com>"
        assert headers["references"] == "<m0@b.com> <m1@b.com>"


# --------------------------------------------------------------------- 正文
class TestExtractBody:
    def test_plain_text(self):
        raw = (
            b"From: a@b.com\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: 8bit\r\n"
            b"\r\n"
            b"\xe4\xbd\xa0\xe5\xa5\xbd\n\xe4\xb8\x96\xe7\x95\x8c\r\n"
        )
        body_text, body_html = extract_body(parse_mime(raw))
        assert body_text == "你好\n世界"
        assert body_html == ""

    def test_html_only_generates_text_fallback(self):
        raw = (
            b"From: a@b.com\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n"
            b"<html><body><p>Hello <b>World</b></p></body></html>\r\n"
        )
        body_text, body_html = extract_body(parse_mime(raw))
        assert "Hello" in body_html and "<b>World</b>" in body_html
        assert body_text == "Hello World"

    def test_multipart_alternative(self):
        raw = (
            b"From: a@b.com\r\n"
            b'Content-Type: multipart/alternative; boundary="ALT"\r\n'
            b"\r\n"
            b"--ALT\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"plain body\r\n"
            b"--ALT\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n"
            b"<p>html body</p>\r\n"
            b"--ALT--\r\n"
        )
        body_text, body_html = extract_body(parse_mime(raw))
        assert body_text == "plain body"
        assert "<p>html body</p>" in body_html

    def test_nested_multipart_mixed_alternative_with_attachment(self):
        filename = "=?utf-8?B?" + _b64_text("报告.pdf") + "?="
        raw = b"".join(
            [
                b"From: a@b.com\r\n",
                b'Content-Type: multipart/mixed; boundary="OUT"\r\n',
                b"\r\n",
                b"--OUT\r\n",
                b'Content-Type: multipart/alternative; boundary="IN"\r\n',
                b"\r\n",
                b"--IN\r\n",
                b"Content-Type: text/plain; charset=utf-8\r\n",
                b"\r\n",
                b"outer text\r\n",
                b"--IN\r\n",
                b"Content-Type: text/html; charset=utf-8\r\n",
                b"\r\n",
                b"<p>outer html</p>\r\n",
                b"--IN--\r\n",
                b"--OUT\r\n",
                b"Content-Type: application/pdf\r\n",
                b"Content-Transfer-Encoding: base64\r\n",
                b'Content-Disposition: attachment; filename="'
                + filename.encode("ascii")
                + b'"\r\n',
                b"\r\n",
                _b64_bytes(b"%PDF-1.4 fake").encode("ascii") + b"\r\n",
                b"--OUT--\r\n",
            ]
        )
        msg = parse_mime(raw)
        body_text, body_html = extract_body(msg)
        assert body_text == "outer text"
        assert "<p>outer html</p>" in body_html

        attachments = extract_attachments(msg)
        assert len(attachments) == 1
        item = attachments[0]
        assert item["filename"] == "报告.pdf"
        assert item["mime"] == "application/pdf"
        assert item["content"] == b"%PDF-1.4 fake"
        assert item["size"] == len(b"%PDF-1.4 fake")
        assert item["inline"] is False

    def test_attachment_text_part_not_used_as_body(self):
        raw = (
            b"From: a@b.com\r\n"
            b'Content-Type: multipart/mixed; boundary="M"\r\n'
            b"\r\n"
            b"--M\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"real body\r\n"
            b"--M\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b'Content-Disposition: attachment; filename="note.txt"\r\n'
            b"\r\n"
            b"attached note\r\n"
            b"--M--\r\n"
        )
        body_text, _ = extract_body(parse_mime(raw))
        assert body_text == "real body"


# --------------------------------------------------------------------- 附件
class TestExtractAttachments:
    def test_base64_attachment_decoded(self):
        payload = b"\x89PNG\r\n\x1a\n fake image bytes"
        raw = b"".join(
            [
                b"From: a@b.com\r\n",
                b'Content-Type: multipart/mixed; boundary="M"\r\n',
                b"\r\n",
                b"--M\r\n",
                b"Content-Type: text/plain\r\n\r\nbody\r\n",
                b"--M\r\n",
                b"Content-Type: image/png\r\n",
                b"Content-Transfer-Encoding: base64\r\n",
                b'Content-Disposition: attachment; filename="screenshot.png"\r\n',
                b"\r\n",
                _b64_bytes(payload).encode("ascii") + b"\r\n",
                b"--M--\r\n",
            ]
        )
        attachments = extract_attachments(parse_mime(raw))
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "screenshot.png"
        assert attachments[0]["content"] == payload
        assert attachments[0]["mime"] == "image/png"

    def test_inline_image_without_filename_gets_generated_name(self):
        raw = b"".join(
            [
                b"From: a@b.com\r\n",
                b'Content-Type: multipart/related; boundary="R"\r\n',
                b"\r\n",
                b"--R\r\n",
                b"Content-Type: text/html; charset=utf-8\r\n",
                b"\r\n",
                b'<img src="cid:img1">\r\n',
                b"--R\r\n",
                b"Content-Type: image/png\r\n",
                b"Content-Transfer-Encoding: base64\r\n",
                b"Content-ID: <img1>\r\n",
                b"\r\n",
                _b64_bytes(b"PNGDATA").encode("ascii") + b"\r\n",
                b"--R--\r\n",
            ]
        )
        attachments = extract_attachments(parse_mime(raw))
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "image001.png"
        assert attachments[0]["inline"] is True

    def test_oversize_attachment_skipped_with_warning(self, caplog):
        big = b"A" * 512
        raw = b"".join(
            [
                b"From: a@b.com\r\n",
                b'Content-Type: multipart/mixed; boundary="M"\r\n',
                b"\r\n",
                b"--M\r\n",
                b"Content-Type: text/plain\r\n\r\nbody\r\n",
                b"--M\r\n",
                b"Content-Type: application/octet-stream\r\n",
                b"Content-Transfer-Encoding: base64\r\n",
                b'Content-Disposition: attachment; filename="big.bin"\r\n',
                b"\r\n",
                _b64_bytes(big).encode("ascii") + b"\r\n",
                b"--M--\r\n",
            ]
        )
        msg = parse_mime(raw)
        with caplog.at_level(logging.WARNING, logger="apps.mailboxes.parser"):
            attachments = extract_attachments(msg, max_size_bytes=100)
        assert attachments == []
        assert any("附件超限" in record.getMessage() for record in caplog.records)

    def test_max_size_uses_setting_when_not_given(self, caplog):
        Setting.objects.update_or_create(
            key="max_attachment_size_mb", defaults={"value": "1"}
        )
        payload = b"B" * (1024 * 1024 + 64)
        raw = b"".join(
            [
                b"From: a@b.com\r\n",
                b'Content-Type: multipart/mixed; boundary="M"\r\n',
                b"\r\n",
                b"--M\r\n",
                b"Content-Type: application/octet-stream\r\n",
                b"Content-Transfer-Encoding: base64\r\n",
                b'Content-Disposition: attachment; filename="huge.bin"\r\n',
                b"\r\n",
                _b64_bytes(payload).encode("ascii") + b"\r\n",
                b"--M--\r\n",
            ]
        )
        msg = parse_mime(raw)
        with caplog.at_level(logging.WARNING, logger="apps.mailboxes.parser"):
            attachments = extract_attachments(msg)
        assert attachments == []
        assert any("附件超限" in record.getMessage() for record in caplog.records)

    def test_attachment_within_setting_limit_kept(self):
        Setting.objects.update_or_create(
            key="max_attachment_size_mb", defaults={"value": "1"}
        )
        raw = (
            b"From: a@b.com\r\n"
            b'Content-Type: multipart/mixed; boundary="M"\r\n'
            b"\r\n"
            b"--M\r\n"
            b"Content-Type: text/plain\r\n"
            b'Content-Disposition: attachment; filename="small.txt"\r\n'
            b"\r\n"
            b"tiny\r\n"
            b"--M--\r\n"
        )
        attachments = extract_attachments(parse_mime(raw))
        assert len(attachments) == 1
        assert attachments[0]["size"] == len(b"tiny")

    def test_no_attachments(self):
        raw = b"From: a@b.com\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        assert extract_attachments(parse_mime(raw)) == []

    def test_calendar_invitation_kept_as_attachment(self):
        raw = b"".join(
            [
                b"From: a@b.com\r\n",
                b'Content-Type: multipart/mixed; boundary="M"\r\n',
                b"\r\n",
                b"--M\r\n",
                b"Content-Type: text/plain; charset=utf-8\r\n",
                b"\r\n",
                b"meeting invite\r\n",
                b"--M\r\n",
                b"Content-Type: text/calendar; method=REQUEST\r\n",
                b"\r\n",
                b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                b"--M--\r\n",
            ]
        )
        msg = parse_mime(raw)
        body_text, _ = extract_body(msg)
        assert body_text == "meeting invite"
        attachments = extract_attachments(msg)
        assert len(attachments) == 1
        assert attachments[0]["mime"] == "text/calendar"
        assert attachments[0]["filename"].endswith(".ics")


# ------------------------------------------------------------- message/rfc822
class TestNestedRfc822:
    def test_attached_eml_kept_as_attachment(self):
        inner = (
            b"From: inner@example.com\r\n"
            b"To: a@b.com\r\n"
            b"Subject: inner subject\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"inner body\r\n"
        )
        raw = (
            b"From: a@b.com\r\n"
            b'Content-Type: multipart/mixed; boundary="M"\r\n'
            b"\r\n"
            b"--M\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"see attached\r\n"
            b"--M\r\n"
            b"Content-Type: message/rfc822\r\n"
            b'Content-Disposition: attachment; filename="forwarded.eml"\r\n'
            b"\r\n"
            + inner
            + b"--M--\r\n"
        )
        msg = parse_mime(raw)
        body_text, _ = extract_body(msg)
        assert body_text == "see attached"

        attachments = extract_attachments(msg)
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "forwarded.eml"
        assert attachments[0]["mime"] == "message/rfc822"
        assert b"inner body" in attachments[0]["content"]

    def test_inline_rfc822_without_filename_parsed_as_body(self):
        raw = (
            b"From: a@b.com\r\n"
            b'Content-Type: multipart/mixed; boundary="M"\r\n'
            b"\r\n"
            b"--M\r\n"
            b"Content-Type: message/rfc822\r\n"
            b"\r\n"
            b"From: inner@example.com\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"inner body text\r\n"
            b"--M--\r\n"
        )
        body_text, _ = extract_body(parse_mime(raw))
        assert "inner body text" in body_text


# ----------------------------------------------------------------- 字符集回退
class TestCharsetFallback:
    def test_iso_8859_1_decoded(self):
        raw = (
            b"From: a@b.com\r\n"
            b"Subject: latin\r\n"
            b"Content-Type: text/plain; charset=iso-8859-1\r\n"
            b"Content-Transfer-Encoding: 8bit\r\n"
            b"\r\n"
            b"caf\xe9 na\xefve\r\n"
        )
        body_text, _ = extract_body(parse_mime(raw))
        assert body_text == "café naïve"

    def test_unknown_charset_falls_back_to_utf8_replace(self):
        raw = (
            b"From: a@b.com\r\n"
            b"Content-Type: text/plain; charset=x-unknown-charset\r\n"
            b"Content-Transfer-Encoding: 8bit\r\n"
            b"\r\n"
            b"hello \xff world\r\n"
        )
        body_text, _ = extract_body(parse_mime(raw))  # 不得抛异常
        assert body_text.startswith("hello ")
        assert body_text.endswith(" world")


# --------------------------------------------------------------- 综合入口
class TestParseInbound:
    def test_parse_inbound_full(self):
        raw = b"".join(
            [
                b"From: Zhang San <ZHANGSAN@Example.com>\r\n",
                b"To: support@example.com, team@example.com\r\n",
                b"Cc: manager@example.com\r\n",
                b"Subject: =?utf-8?B?" + _b64_text("退款申请").encode("ascii") + b"?=\r\n",
                b"Message-ID: <m9@example.com>\r\n",
                b"In-Reply-To: <m8@example.com>\r\n",
                b"References: <m7@example.com> <m8@example.com>\r\n",
                b"Date: Mon, 01 Jan 2024 10:00:00 +0800\r\n",
                b'Content-Type: multipart/mixed; boundary="M"\r\n',
                b"\r\n",
                b"--M\r\n",
                b"Content-Type: text/plain; charset=utf-8\r\n",
                b"\r\n",
                "我要退款\r\n".encode("utf-8"),
                b"--M\r\n",
                b"Content-Type: text/html; charset=utf-8\r\n",
                b"\r\n",
                "<p>我要退款</p>\r\n".encode("utf-8"),
                b"--M\r\n",
                b"Content-Type: text/plain\r\n",
                b'Content-Disposition: attachment; filename="order.txt"\r\n',
                b"\r\n",
                b"order-123\r\n",
                b"--M--\r\n",
            ]
        )
        data = parse_inbound(raw)

        assert data["message_id"] == "<m9@example.com>"
        assert data["in_reply_to"] == "<m8@example.com>"
        assert data["references"] == "<m7@example.com> <m8@example.com>"
        # from_addr 经 extract_email 归一化为小写
        assert data["from_addr"] == "zhangsan@example.com"
        # to_addr / cc_addr 保留原始头字符串
        assert data["to_addr"] == "support@example.com, team@example.com"
        assert data["cc_addr"] == "manager@example.com"
        assert data["subject"] == "退款申请"
        assert data["sent_at"] is not None
        assert data["sent_at"].year == 2024
        assert data["sent_at"].tzinfo is not None
        assert data["body_text"] == "我要退款"
        assert "<p>我要退款</p>" in data["body_html"]
        assert len(data["attachments"]) == 1
        assert data["attachments"][0]["filename"] == "order.txt"
        assert data["headers"] == extract_headers(parse_mime(raw))

    def test_parse_inbound_missing_optional_headers(self):
        raw = b"From: a@b.com\r\n\r\nbody\r\n"
        data = parse_inbound(raw)
        assert data["message_id"] == ""
        assert data["sent_at"] is None
        assert data["subject"] == ""
        assert data["attachments"] == []
