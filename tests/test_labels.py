"""§12.6 待回复标签验收用例。"""

from __future__ import annotations

from apps.tickets.services import on_inbound, on_outbound
from tests.conftest import make_ticket

CUSTOMER = "customer@customer-domain.com"


def test_inbound_sets_awaiting_reply_true(unified_mailbox, tech_group):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    on_inbound(ticket)
    ticket.refresh_from_db()
    assert ticket.is_awaiting_reply is True


def test_outbound_sets_awaiting_reply_false(unified_mailbox, tech_group):
    ticket = make_ticket(
        mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER, is_awaiting_reply=True
    )
    on_outbound(ticket)
    ticket.refresh_from_db()
    assert ticket.is_awaiting_reply is False


def test_inbound_after_reply_sets_true_again(unified_mailbox, tech_group):
    ticket = make_ticket(
        mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER, is_awaiting_reply=True
    )
    on_outbound(ticket)
    on_inbound(ticket)
    ticket.refresh_from_db()
    assert ticket.is_awaiting_reply is True
