"""§12.1 工单归并验收用例。"""

from __future__ import annotations

from apps.tickets.services import find_ticket
from tests.conftest import make_message, make_ticket
from tests.helpers import build_email, message_id_of

CUSTOMER = "customer@customer-domain.com"
OTHER = "other@another-domain.com"


def test_merge_by_references_same_sender(unified_mailbox, tech_group):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    msg = build_email(message_id="<m-1@customer-domain.com>")
    make_message(ticket=ticket, mailbox=unified_mailbox, message_id=message_id_of(msg))

    reply = build_email(sender=CUSTOMER, references="<m-1@customer-domain.com>")
    assert find_ticket(reply, unified_mailbox) == ticket


def test_merge_by_references_different_sender(unified_mailbox, tech_group):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    msg = build_email(message_id="<m-2@customer-domain.com>")
    make_message(ticket=ticket, mailbox=unified_mailbox, message_id=message_id_of(msg))

    reply = build_email(sender=OTHER, references="<m-2@customer-domain.com>")
    assert find_ticket(reply, unified_mailbox) is None


def test_merge_by_ticket_no_same_sender(unified_mailbox, tech_group):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    reply = build_email(sender=CUSTOMER, subject=f"[T#{ticket.pk}] 无法登录后台 Re:")
    assert find_ticket(reply, unified_mailbox) == ticket


def test_merge_by_ticket_no_different_sender(unified_mailbox, tech_group):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    reply = build_email(sender=OTHER, subject=f"[T#{ticket.pk}] 无法登录后台")
    # 发件人不一致 → 视为新工单，不归并（§2.2 工单号校验）
    assert find_ticket(reply, unified_mailbox) is None


def test_merge_references_priority_over_subject(unified_mailbox, tech_group, finance_group):
    by_reference = make_ticket(
        mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER, subject="引用优先"
    )
    by_subject = make_ticket(
        mailbox=unified_mailbox, group=finance_group, customer_email=CUSTOMER, subject="主题工单号"
    )
    msg = build_email(message_id="<m-3@customer-domain.com>")
    make_message(ticket=by_reference, mailbox=unified_mailbox, message_id=message_id_of(msg))

    reply = build_email(
        sender=CUSTOMER,
        subject=f"[T#{by_subject.pk}] 主题工单号",
        references="<m-3@customer-domain.com>",
    )
    assert find_ticket(reply, unified_mailbox) == by_reference


def test_no_merge_when_nothing_matches(unified_mailbox, tech_group):
    make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    unrelated = build_email(sender=CUSTOMER, subject="全新的问题", references="<unknown@id>")
    assert find_ticket(unrelated, unified_mailbox) is None
