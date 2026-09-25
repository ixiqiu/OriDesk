"""``apps.mailboxes.filters`` 单元测试（开发文档 §6.1 / §12.4）。

覆盖 §12.4 的 5 个防循环用例（auto-submitted、precedence bulk、from own
mailbox、from noreply、正常邮件不误杀）以及退信识别与其余 §6.1 条件。
"""

import pytest

from apps.accounts.models import Mailbox
from apps.mailboxes.filters import (
    REASON_AUTO_RESPONSE_SUPPRESS,
    REASON_AUTO_SUBMITTED,
    REASON_BOUNCE,
    REASON_LIST_ID,
    REASON_NOREPLY_SENDER,
    REASON_OWN_MAILBOX,
    REASON_PRECEDENCE,
    is_bounce_mail,
    is_loop_mail,
    loop_reason,
)
from apps.mailboxes.parser import parse_mime

pytestmark = pytest.mark.django_db


def _msg(headers: dict, body: str = "邮件正文", *, raw_headers: str = ""):
    """把 header 字典拼成 raw MIME 后解析，顺带覆盖 parse_mime 链路。"""
    lines = [f"{key}: {value}" for key, value in headers.items()]
    head = "\r\n".join(lines)
    if raw_headers:
        head = f"{head}\r\n{raw_headers}"
    raw = f"{head}\r\n\r\n{body}".encode("utf-8")
    return parse_mime(raw)


def _own_mailbox(email: str = "support@example.com") -> Mailbox:
    return Mailbox.objects.create(
        name="客服邮箱",
        email=email,
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username=email,
        secret_encrypted=b"encrypted",
    )


def _normal_headers(**overrides) -> dict:
    headers = {
        "From": "客户 <customer@example.com>",
        "To": "support@example.com",
        "Subject": "无法登录",
        "Message-ID": "<abc123@example.com>",
        "Date": "Mon, 01 Jan 2024 10:00:00 +0800",
    }
    headers.update(overrides)
    return headers


# ------------------------------------------------------------- §12.4 五个用例
class TestLoopMailDocCases:
    def test_loop_mail_auto_submitted(self):
        """§12.4 test_loop_mail_auto_submitted → 丢弃。"""
        msg = _msg(_normal_headers(**{"Auto-Submitted": "auto-generated"}))
        assert is_loop_mail(msg) is True
        assert loop_reason(msg) == REASON_AUTO_SUBMITTED

    def test_loop_mail_auto_replied(self):
        msg = _msg(_normal_headers(**{"Auto-Submitted": "auto-replied"}))
        assert is_loop_mail(msg) is True
        assert loop_reason(msg) == REASON_AUTO_SUBMITTED

    def test_loop_mail_precedence_bulk(self):
        """§12.4 test_loop_mail_precedence_bulk → 丢弃。"""
        msg = _msg(_normal_headers(Precedence="bulk"))
        assert is_loop_mail(msg) is True
        assert loop_reason(msg) == REASON_PRECEDENCE

    @pytest.mark.parametrize("value", ["junk", "list"])
    def test_loop_mail_precedence_other_values(self, value):
        msg = _msg(_normal_headers(Precedence=value))
        assert is_loop_mail(msg) is True
        assert loop_reason(msg) == REASON_PRECEDENCE

    def test_loop_mail_from_own_mailbox(self):
        """§12.4 test_loop_mail_from_own_mailbox → 丢弃（防止自回自）。"""
        mailbox = _own_mailbox()
        msg = _msg(_normal_headers(From=f"客服 <{mailbox.email}>"))
        assert is_loop_mail(msg) is True
        assert loop_reason(msg) == REASON_OWN_MAILBOX

    def test_loop_mail_from_own_mailbox_case_insensitive(self):
        _own_mailbox("Support@Example.com")
        msg = _msg(_normal_headers(From="客服 <support@example.com>"))
        assert is_loop_mail(msg) is True
        assert loop_reason(msg) == REASON_OWN_MAILBOX

    def test_loop_mail_from_own_mailbox_with_explicit_model(self):
        mailbox = _own_mailbox()
        msg = _msg(_normal_headers(From=mailbox.email))
        assert is_loop_mail(msg, mailbox_model=Mailbox) is True

    def test_loop_mail_from_noreply(self):
        """§12.4 test_loop_mail_from_noreply → 丢弃。"""
        msg = _msg(_normal_headers(From="系统通知 <no-reply@example.com>"))
        assert is_loop_mail(msg) is True
        assert loop_reason(msg) == REASON_NOREPLY_SENDER

    @pytest.mark.parametrize(
        "sender",
        [
            "noreply@example.com",
            "no-reply@example.com",
            "mailer-daemon@example.com",
            "postmaster@example.com",
        ],
    )
    def test_loop_mail_noreply_variants(self, sender):
        msg = _msg(_normal_headers(From=sender))
        assert is_loop_mail(msg) is True
        assert loop_reason(msg) == REASON_NOREPLY_SENDER

    def test_normal_mail_not_filtered(self):
        """§12.4 test_normal_mail_not_filtered → 正常处理。"""
        msg = _msg(_normal_headers())
        assert is_loop_mail(msg) is False
        assert loop_reason(msg) is None


# --------------------------------------------------------------- §6.1 其余条件
class TestOtherLoopConditions:
    def test_x_auto_response_suppress_all(self):
        msg = _msg(_normal_headers(**{"X-Auto-Response-Suppress": "All"}))
        assert is_loop_mail(msg) is True
        assert loop_reason(msg) == REASON_AUTO_RESPONSE_SUPPRESS

    def test_x_auto_response_suppress_other_value_not_loop(self):
        msg = _msg(_normal_headers(**{"X-Auto-Response-Suppress": "OOF"}))
        assert is_loop_mail(msg) is False

    def test_list_id(self):
        msg = _msg(_normal_headers(**{"List-Id": "公告 <announce.example.com>"}))
        assert is_loop_mail(msg) is True
        assert loop_reason(msg) == REASON_LIST_ID

    def test_own_mailbox_not_matched_when_not_registered(self):
        """未登记为系统邮箱的地址不误杀。"""
        _own_mailbox("support@example.com")
        msg = _msg(_normal_headers(From="someone@other.example.com"))
        assert is_loop_mail(msg) is False

    def test_no_mailbox_rows_does_not_touch_db_incorrectly(self):
        msg = _msg(_normal_headers())
        assert Mailbox.objects.count() == 0
        assert is_loop_mail(msg) is False


# ------------------------------------------------------------------- 退信识别
class TestBounceDetection:
    def test_multipart_report_delivery_status(self):
        raw = (
            b"From: Mail Delivery System <dsn@relay.example.com>\r\n"
            b"To: customer@example.com\r\n"
            b"Subject: Undelivered Mail Returned to Sender\r\n"
            b'Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r\n'
            b"\r\n"
            b"--B\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Delivery failed.\r\n"
            b"--B--\r\n"
        )
        msg = parse_mime(raw)
        assert is_bounce_mail(msg) is True
        assert loop_reason(msg) == REASON_BOUNCE
        assert is_loop_mail(msg) is True

    def test_from_mailer_daemon(self):
        msg = _msg(_normal_headers(From="MAILER-DAEMON@example.com"))
        assert is_bounce_mail(msg) is True
        assert is_loop_mail(msg) is True

    def test_empty_return_path(self):
        msg = _msg(_normal_headers(**{"Return-Path": "<>"}))
        assert is_bounce_mail(msg) is True
        assert is_loop_mail(msg) is True

    def test_normal_mail_is_not_bounce(self):
        msg = _msg(_normal_headers())
        assert is_bounce_mail(msg) is False

    def test_multipart_report_without_delivery_status_is_not_bounce(self):
        raw = (
            b"From: customer@example.com\r\n"
            b"Subject: report\r\n"
            b'Content-Type: multipart/report; report-type=disposition-notification; boundary="B"\r\n'
            b"\r\n"
            b"--B\r\n"
            b"Content-Type: text/plain\r\n\r\nhi\r\n"
            b"--B--\r\n"
        )
        assert is_bounce_mail(parse_mime(raw)) is False
