"""§12.2 路由引擎验收用例（粘性 / 规则优先级 / 兜底 / 管理员组）。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.audit.models import Setting
from apps.routing.models import Rule
from apps.routing.services import route_ticket
from tests.conftest import make_ticket
from tests.helpers import build_email

CUSTOMER = "customer@customer-domain.com"


def test_sticky_within_window(unified_mailbox, tech_group, finance_group):
    now = timezone.now()
    Setting.set("sticky_window_days", 7)  # 显式固定窗口，避免依赖默认值或被其他用例污染
    make_ticket(
        mailbox=unified_mailbox,
        group=tech_group,
        customer_email=CUSTOMER,
        last_message_at=now - timedelta(days=4),
    )
    Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="发票",
        action_type="assign_group",
        action_value=str(finance_group.pk),
    )

    msg = build_email(sender=CUSTOMER, subject="发票问题")
    # 4 天前有往来（窗口 7 天）→ 粘性优先，仍归技术支持组
    assert route_ticket(msg, CUSTOMER, unified_mailbox, now=now) == tech_group


def test_sticky_window_boundary_is_inclusive(unified_mailbox, tech_group, finance_group):
    """边界语义固定为含等号：正好 N 天前算窗口内。"""
    now = timezone.now()
    Setting.set("sticky_window_days", 7)
    make_ticket(
        mailbox=unified_mailbox,
        group=tech_group,
        customer_email=CUSTOMER,
        last_message_at=now - timedelta(days=7),
    )
    msg = build_email(sender=CUSTOMER, subject="任意主题")
    assert route_ticket(msg, CUSTOMER, unified_mailbox, now=now) == tech_group


@pytest.mark.parametrize("window", [0, -1])
def test_sticky_disabled_for_non_positive_window(
    unified_mailbox, tech_group, finance_group, admin_group, window
):
    """窗口设为 0 或负数表示关闭粘性，不得因 >= 含等号而误判。"""
    now = timezone.now()
    Setting.set("sticky_window_days", window)
    make_ticket(
        mailbox=unified_mailbox,
        group=tech_group,
        customer_email=CUSTOMER,
        last_message_at=now,
    )
    Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="发票",
        action_type="assign_group",
        action_value=str(finance_group.pk),
    )
    msg = build_email(sender=CUSTOMER, subject="发票问题")
    assert route_ticket(msg, CUSTOMER, unified_mailbox, now=now) == finance_group


def test_sticky_outside_window(unified_mailbox, tech_group, finance_group):
    now = timezone.now()
    Setting.set("sticky_window_days", 7)
    make_ticket(
        mailbox=unified_mailbox,
        group=tech_group,
        customer_email=CUSTOMER,
        last_message_at=now - timedelta(days=10),
    )
    Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="发票",
        action_type="assign_group",
        action_value=str(finance_group.pk),
    )

    msg = build_email(sender=CUSTOMER, subject="发票问题")
    # 超过粘性窗口 → 按主题重新路由到财务组
    assert route_ticket(msg, CUSTOMER, unified_mailbox, now=now) == finance_group


def test_sticky_based_on_last_message_at(unified_mailbox, tech_group, finance_group):
    """工单创建很久，但最近仍有往来 → 不应误判为长期无粘性。"""
    now = timezone.now()
    ticket = make_ticket(
        mailbox=unified_mailbox,
        group=tech_group,
        customer_email=CUSTOMER,
        last_message_at=now - timedelta(hours=2),
    )
    # 人为把创建时间改到 60 天前，只有 last_message_at 是新的
    type(ticket).objects.filter(pk=ticket.pk).update(created_at=now - timedelta(days=60))
    Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="发票",
        action_type="assign_group",
        action_value=str(finance_group.pk),
    )

    msg = build_email(sender=CUSTOMER, subject="发票问题")
    assert route_ticket(msg, CUSTOMER, unified_mailbox, now=now) == tech_group


def test_rule_priority_first_match_wins(unified_mailbox, tech_group, finance_group):
    Rule.objects.create(
        mailbox=unified_mailbox,
        priority=20,
        match_field="subject",
        match_op="contains",
        match_value="登录",
        action_type="assign_group",
        action_value=str(tech_group.pk),
    )
    Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="登录",
        action_type="assign_group",
        action_value=str(finance_group.pk),
    )

    msg = build_email(sender=CUSTOMER, subject="无法登录")
    assert route_ticket(msg, CUSTOMER, unified_mailbox) == finance_group


def test_fallback_group_when_no_rule_matches(unified_mailbox, tech_group, admin_group):
    Setting.set("fallback_group_id", tech_group.pk)
    msg = build_email(sender=CUSTOMER, subject="随便一个主题")
    assert route_ticket(msg, CUSTOMER, unified_mailbox) == tech_group


def test_admin_group_when_no_fallback(unified_mailbox, admin_group, tech_group):
    Setting.set("fallback_group_id", "")
    msg = build_email(sender=CUSTOMER, subject="随便一个主题")
    assert route_ticket(msg, CUSTOMER, unified_mailbox) == admin_group


def test_group_mailbox_enters_own_group_directly(tech_mailbox, tech_group, finance_group):
    """§2.2：发到组邮箱且不能归并 → 直接进入该组，不走主题规则。"""
    Rule.objects.create(
        mailbox=tech_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="发票",
        action_type="assign_group",
        action_value=str(finance_group.pk),
    )
    msg = build_email(sender=CUSTOMER, to=tech_mailbox.email, subject="发票问题")
    assert route_ticket(msg, CUSTOMER, tech_mailbox) == tech_group


def test_route_without_any_group_raises(unified_mailbox):
    from apps.routing.services import RoutingError

    msg = build_email(sender=CUSTOMER, subject="无组可用")
    with pytest.raises(RoutingError):
        route_ticket(msg, CUSTOMER, unified_mailbox)
