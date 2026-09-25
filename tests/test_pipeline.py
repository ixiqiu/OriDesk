"""§6.5 入站处理流水线端到端验收用例。

覆盖：建单+路由+自动回复、防循环丢弃、归并保持原组、幂等去重、
附件落盘、HTML 净化、组邮箱直接进入该组。
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest
from django.utils import timezone

from apps.mailboxes.pipeline import process_inbound
from apps.routing.models import Rule
from apps.tickets.models import Attachment, Message, Ticket
from tests.helpers import build_raw

CUSTOMER = "customer@customer-domain.com"


@pytest.fixture
def media_root(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


@pytest.fixture
def login_rule(unified_mailbox, tech_group):
    return Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="登录",
        action_type="assign_group",
        action_value=str(tech_group.pk),
    )


def test_pipeline_creates_ticket_and_sends_auto_reply(
    unified_mailbox, tech_group, login_rule, outbox
):
    result = process_inbound(
        unified_mailbox, 1, build_raw(sender=CUSTOMER, subject="无法登录后台", body="请帮忙看看")
    )

    assert result.status == "processed"
    assert result.created is True
    assert result.auto_replied is True

    ticket = result.ticket
    assert ticket.group == tech_group
    assert ticket.customer_email == CUSTOMER
    assert ticket.normalized_subject == "无法登录后台"
    assert ticket.is_awaiting_reply is True
    assert ticket.messages.filter(direction="in").count() == 1
    assert len(outbox) == 1
    assert outbox[0]["mailbox"] == unified_mailbox


def test_pipeline_loop_mail_is_dropped(unified_mailbox, tech_group, login_rule, outbox):
    raw = build_raw(
        sender=CUSTOMER,
        subject="自动回复",
        headers={"Auto-Submitted": "auto-generated"},
    )
    result = process_inbound(unified_mailbox, 2, raw)

    assert result.status == "skipped_loop"
    assert Ticket.objects.count() == 0
    assert outbox == []


def test_pipeline_loop_mail_from_own_mailbox_is_dropped(
    unified_mailbox, tech_group, login_rule, outbox
):
    raw = build_raw(sender=unified_mailbox.email, subject="无法登录后台")
    result = process_inbound(unified_mailbox, 3, raw)

    assert result.status == "skipped_loop"
    assert Ticket.objects.count() == 0


def test_pipeline_merges_reply_and_keeps_group(
    unified_mailbox, tech_group, finance_group, login_rule, outbox
):
    first = process_inbound(unified_mailbox, 4, build_raw(sender=CUSTOMER, subject="无法登录后台"))
    ticket = first.ticket
    original_message_id = ticket.messages.first().message_id

    # 后续来信主题命中财务规则，但应归并到原工单、保持原组（§2.2 / §2.3）
    Rule.objects.create(
        mailbox=unified_mailbox,
        priority=5,
        match_field="subject",
        match_op="contains",
        match_value="登录",
        action_type="assign_group",
        action_value=str(finance_group.pk),
    )
    second = process_inbound(
        unified_mailbox,
        5,
        build_raw(
            sender=CUSTOMER,
            subject="Re: 无法登录后台",
            body="还是不行",
            references=f"<{original_message_id}>",
        ),
    )

    ticket.refresh_from_db()
    assert second.created is False
    assert second.auto_replied is False
    assert ticket.group == tech_group
    assert ticket.messages.filter(direction="in").count() == 2
    assert len(outbox) == 1  # 归并的来信不触发自动回复


def test_pipeline_deduplicates_same_message_id(unified_mailbox, login_rule, outbox):
    raw = build_raw(sender=CUSTOMER, subject="无法登录后台", message_id="<dup-1@customer-domain.com>")
    first = process_inbound(unified_mailbox, 6, raw)
    second = process_inbound(unified_mailbox, 7, raw)

    assert first.status == "processed"
    assert second.status == "skipped_duplicate"
    assert Ticket.objects.count() == 1
    assert Message.objects.filter(direction="in").count() == 1


def test_pipeline_stores_attachment(unified_mailbox, login_rule, media_root, outbox):
    msg = EmailMessage()
    msg["From"] = CUSTOMER
    msg["To"] = unified_mailbox.email
    msg["Subject"] = "无法登录后台"
    msg["Message-ID"] = "<with-attachment@customer-domain.com>"
    msg.set_content("附件是截图")
    msg.add_attachment(b"\x89PNG fake image", maintype="image", subtype="png", filename="截图.png")

    result = process_inbound(unified_mailbox, 8, msg.as_bytes())

    attachment = Attachment.objects.get(message=result.message)
    assert attachment.filename == "截图.png"
    assert attachment.size == len(b"\x89PNG fake image")
    assert attachment.path.startswith(f"attachments/{result.ticket.pk}/")
    assert (media_root / attachment.path).exists()
    assert attachment.is_dangerous is False


def test_pipeline_marks_dangerous_attachment(unified_mailbox, login_rule, media_root, outbox):
    msg = EmailMessage()
    msg["From"] = CUSTOMER
    msg["To"] = unified_mailbox.email
    msg["Subject"] = "无法登录后台"
    msg["Message-ID"] = "<evil@customer-domain.com>"
    msg.set_content("看附件")
    msg.add_attachment(b"MZ fake exe", maintype="application", subtype="octet-stream", filename="tool.exe")

    result = process_inbound(unified_mailbox, 9, msg.as_bytes())
    attachment = Attachment.objects.get(message=result.message)
    assert attachment.is_dangerous is True


def test_pipeline_sanitizes_html(unified_mailbox, login_rule, outbox):
    raw = build_raw(
        sender=CUSTOMER,
        subject="无法登录后台",
        html='<p>你好</p><script>alert(1)</script><img src="x" onerror="alert(2)">',
    )
    result = process_inbound(unified_mailbox, 10, raw)

    html = result.message.body_html
    assert "<script" not in html.lower()
    assert "onerror" not in html.lower()
    assert "你好" in html


def test_pipeline_group_mailbox_enters_own_group(
    tech_mailbox, tech_group, finance_group, outbox
):
    Rule.objects.create(
        mailbox=tech_mailbox,
        priority=1,
        match_field="subject",
        match_op="contains",
        match_value="发票",
        action_type="assign_group",
        action_value=str(finance_group.pk),
    )
    result = process_inbound(
        tech_mailbox, 11, build_raw(sender=CUSTOMER, to=tech_mailbox.email, subject="发票问题")
    )
    assert result.ticket.group == tech_group


def test_pipeline_skips_when_no_group_available(unified_mailbox, outbox):
    """系统未配置任何用户组时抛出 RoutingError（不生成孤儿工单）。"""
    from apps.routing.services import RoutingError

    with pytest.raises(RoutingError):
        process_inbound(unified_mailbox, 12, build_raw(sender=CUSTOMER, subject="无处可去"))
    assert Ticket.objects.count() == 0


def test_pipeline_updates_last_message_at(unified_mailbox, login_rule, outbox):
    now = timezone.now()
    result = process_inbound(unified_mailbox, 13, build_raw(sender=CUSTOMER), now=now)
    assert result.ticket.last_message_at == now
