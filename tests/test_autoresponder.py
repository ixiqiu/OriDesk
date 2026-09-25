"""§12.3 自动回复验收用例。"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.autoresponder.services import send_auto_reply, should_auto_reply
from apps.tickets.models import Message
from tests.conftest import make_ticket

CUSTOMER = "customer@customer-domain.com"


def test_auto_reply_on_first_contact(unified_mailbox):
    assert should_auto_reply(None, CUSTOMER) is True


def test_no_auto_reply_on_merged_ticket(unified_mailbox, tech_group):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    assert should_auto_reply(ticket, CUSTOMER) is False


def test_no_auto_reply_within_window(unified_mailbox, tech_group):
    now = timezone.now()
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    type(ticket).objects.filter(pk=ticket.pk).update(created_at=now - timedelta(hours=2))
    assert should_auto_reply(None, CUSTOMER, now=now) is False


def test_auto_reply_after_window(unified_mailbox, tech_group):
    now = timezone.now()
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    type(ticket).objects.filter(pk=ticket.pk).update(created_at=now - timedelta(hours=48))
    # 默认窗口 24 小时，48 小时前建过单 → 视为首次进线
    assert should_auto_reply(None, CUSTOMER, now=now) is True


def test_auto_reply_recorded_as_message(unified_mailbox, tech_group, outbox):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    message = send_auto_reply(ticket, unified_mailbox)

    assert message is not None
    assert message.is_auto_reply is True
    assert message.direction == "out"
    assert Message.objects.filter(ticket=ticket, is_auto_reply=True).count() == 1
    assert len(outbox) == 1


def test_auto_reply_has_auto_submitted_header(unified_mailbox, tech_group, outbox):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    send_auto_reply(ticket, unified_mailbox)

    sent = outbox[0]["msg"]
    assert sent["Auto-Submitted"] == "auto-replied"
    assert sent["Precedence"] == "auto_reply"
    assert sent["Reply-To"] == unified_mailbox.email
    assert f"[T#{ticket.pk}]" in sent["Subject"]


def test_auto_reply_not_sent_twice(unified_mailbox, tech_group, outbox):
    """防重复（§7-4）：同一工单只发一条自动回复。"""
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    assert send_auto_reply(ticket, unified_mailbox) is not None
    assert send_auto_reply(ticket, unified_mailbox) is None
    assert len(outbox) == 1


def test_group_template_overrides_global(unified_mailbox, tech_group, tech_mailbox):
    from apps.routing.models import Template

    Template.objects.create(scope="global", body="全局模板 {{ ticket_id }}")
    Template.objects.create(scope="group", group=tech_group, body="组专属模板 {{ ticket_id }}")

    ticket = make_ticket(mailbox=tech_mailbox, group=tech_group, customer_email=CUSTOMER)
    assert send_auto_reply(ticket, tech_mailbox) is not None
    message = Message.objects.filter(ticket=ticket, is_auto_reply=True).first()
    assert message.body_text == f"组专属模板 {ticket.pk}"
