"""§12.5 发信与审计验收用例。"""

from __future__ import annotations

from apps.audit.models import AuditLog
from apps.mailboxes.services import send_reply
from apps.tickets.models import Message
from tests.conftest import make_ticket

CUSTOMER = "customer@customer-domain.com"


def test_reply_uses_group_mailbox(tech_group, tech_mailbox, tech_user, unified_mailbox, outbox):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    message = send_reply(ticket, tech_user, "已为你重置密码。")

    sent = outbox[0]["msg"]
    assert sent["From"] == "tech@example.com"
    assert sent["Reply-To"] == "tech@example.com"
    assert message.mailbox == tech_mailbox
    assert message.from_addr == "tech@example.com"
    # 内部记录实际发件人，对外身份为组邮箱（§2.5）
    assert message.actual_sender == tech_user
    assert message.direction == "out"


def test_reply_uses_fallback_for_no_mailbox_group(
    finance_group, fallback_mailbox, finance_user, unified_mailbox, outbox
):
    assert finance_group.mailbox is None
    ticket = make_ticket(mailbox=unified_mailbox, group=finance_group, customer_email=CUSTOMER)
    message = send_reply(ticket, finance_user, "请查收发票。")

    assert outbox[0]["msg"]["From"] == "fallback@example.com"
    assert message.mailbox == fallback_mailbox
    assert message.actual_sender == finance_user


def test_reply_records_actual_sender(tech_group, tech_mailbox, tech_user, unified_mailbox):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    message = send_reply(ticket, tech_user, "已处理。")
    stored = Message.objects.get(pk=message.pk)
    assert stored.actual_sender_id == tech_user.pk
    assert stored.type == "message"


def test_reply_writes_audit_log(tech_group, tech_mailbox, tech_user, unified_mailbox):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    send_reply(ticket, tech_user, "已处理。")

    log = AuditLog.objects.filter(ticket=ticket, action="reply").first()
    assert log is not None
    assert log.user == tech_user
    assert log.group == tech_group
    assert log.identity_email == "tech@example.com"
    assert log.detail["to"] == CUSTOMER


def test_reply_sets_awaiting_reply_false(tech_group, tech_mailbox, tech_user, unified_mailbox):
    ticket = make_ticket(
        mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER, is_awaiting_reply=True
    )
    send_reply(ticket, tech_user, "已处理。")
    ticket.refresh_from_db()
    assert ticket.is_awaiting_reply is False


def test_reply_subject_contains_ticket_no(tech_group, tech_mailbox, tech_user, unified_mailbox, outbox):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    send_reply(ticket, tech_user, "已处理。")
    assert outbox[0]["msg"]["Subject"].startswith(f"[T#{ticket.pk}]")


def test_reply_sets_threading_headers(tech_group, tech_mailbox, tech_user, unified_mailbox, outbox):
    from tests.conftest import make_message

    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    inbound = make_message(
        ticket=ticket, mailbox=unified_mailbox, message_id="inbound-1@customer-domain.com"
    )
    send_reply(ticket, tech_user, "已处理。")

    sent = outbox[0]["msg"]
    assert sent["In-Reply-To"] == inbound.message_id
    assert inbound.message_id in sent["References"]
