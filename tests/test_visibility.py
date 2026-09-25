"""§12.7 可见性验收用例（开发文档 §5.6 / §2.7）。"""

from __future__ import annotations

from apps.tickets.selectors import visible_tickets
from tests.conftest import make_ticket

CUSTOMER = "customer@customer-domain.com"


def _two_tickets(unified_mailbox, tech_group, finance_group):
    tech_ticket = make_ticket(
        mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER, subject="技术问题"
    )
    finance_ticket = make_ticket(
        mailbox=unified_mailbox, group=finance_group, customer_email=CUSTOMER, subject="发票问题"
    )
    return tech_ticket, finance_ticket


def test_superadmin_sees_all(unified_mailbox, tech_group, finance_group, superadmin):
    _two_tickets(unified_mailbox, tech_group, finance_group)
    assert visible_tickets(superadmin).count() == 2


def test_admin_group_member_sees_all(unified_mailbox, tech_group, finance_group, admin_group_user):
    _two_tickets(unified_mailbox, tech_group, finance_group)
    assert visible_tickets(admin_group_user).count() == 2


def test_normal_user_sees_own_groups_only(unified_mailbox, tech_group, finance_group, tech_user):
    tech_ticket, finance_ticket = _two_tickets(unified_mailbox, tech_group, finance_group)
    ids = set(visible_tickets(tech_user).values_list("pk", flat=True))
    assert ids == {tech_ticket.pk}


def test_user_not_in_group_cannot_see(unified_mailbox, tech_group, finance_group, django_user_model):
    _two_tickets(unified_mailbox, tech_group, finance_group)
    outsider = django_user_model.objects.create_user(username="outsider", password="DemoPass!2345")
    assert visible_tickets(outsider).count() == 0


def test_group_admin_member_sees_own_group_only(unified_mailbox, tech_group, finance_group, django_user_model):
    """组内管理员（is_admin=True）按 §5.6 可跨组查看。"""
    from apps.accounts.models import UserGroup

    tech_ticket, finance_ticket = _two_tickets(unified_mailbox, tech_group, finance_group)
    user = django_user_model.objects.create_user(username="techlead", password="DemoPass!2345")
    UserGroup.objects.create(user=user, group=tech_group, is_admin=True)
    assert visible_tickets(user).count() == 2
