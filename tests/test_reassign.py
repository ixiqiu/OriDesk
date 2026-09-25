"""§12.8 改派验收用例（开发文档 §2.2 改派规则）。"""

from __future__ import annotations

from apps.audit.models import AuditLog
from apps.routing.services import reassign_ticket
from apps.tickets.selectors import visible_tickets
from tests.conftest import make_message, make_ticket
from tests.helpers import build_email, message_id_of

CUSTOMER = "customer@customer-domain.com"


def test_reassign_changes_group(unified_mailbox, tech_group, finance_group, tech_user):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    reassign_ticket(ticket, finance_group, tech_user, reason="属于财务范围")
    ticket.refresh_from_db()
    assert ticket.group == finance_group


def test_reassign_original_group_loses_visibility(
    unified_mailbox, tech_group, finance_group, tech_user, finance_user
):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    reassign_ticket(ticket, finance_group, tech_user)

    assert visible_tickets(tech_user).filter(pk=ticket.pk).exists() is False
    assert visible_tickets(finance_user).filter(pk=ticket.pk).exists() is True


def test_reassign_admin_still_visible(unified_mailbox, tech_group, finance_group, tech_user, superadmin, admin_group_user):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    reassign_ticket(ticket, finance_group, tech_user)

    assert visible_tickets(superadmin).filter(pk=ticket.pk).exists() is True
    assert visible_tickets(admin_group_user).filter(pk=ticket.pk).exists() is True


def test_reassign_writes_audit(unified_mailbox, tech_group, finance_group, tech_user):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    reassign_ticket(ticket, finance_group, tech_user, reason="客户属于财务")

    log = AuditLog.objects.filter(ticket=ticket, action="forward").first()
    assert log is not None
    assert log.user == tech_user
    assert log.group == finance_group
    assert log.detail["from_group"] == "技术支持组"
    assert log.detail["to_group"] == "财务组"
    assert log.detail["reason"] == "客户属于财务"


def test_reassign_clears_assignee(unified_mailbox, tech_group, finance_group, tech_user):
    ticket = make_ticket(
        mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER, assignee=tech_user
    )
    reassign_ticket(ticket, finance_group, tech_user)
    ticket.refresh_from_db()
    assert ticket.assignee is None


def test_reassign_customer_reply_stays_in_new_group(
    unified_mailbox, tech_group, finance_group, tech_user
):
    """改派后客户回复：归并原工单，保持新组归属（§2.2）。"""
    from apps.tickets.services import find_ticket

    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    inbound = build_email(sender=CUSTOMER, message_id="<orig@customer-domain.com>")
    make_message(ticket=ticket, mailbox=unified_mailbox, message_id=message_id_of(inbound))
    reassign_ticket(ticket, finance_group, tech_user)

    reply = build_email(sender=CUSTOMER, references="<orig@customer-domain.com>")
    merged = find_ticket(reply, unified_mailbox)
    assert merged == ticket
    assert merged.group == finance_group
