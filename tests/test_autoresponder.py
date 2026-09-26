"""§12.3 自动回复验收用例。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.autoresponder.services import send_auto_reply, should_auto_reply
from apps.mailboxes.pipeline import process_inbound
from apps.tickets.models import Message
from tests.conftest import make_ticket
from tests.helpers import build_raw

CUSTOMER = "customer@customer-domain.com"


@pytest.fixture
def login_rule(unified_mailbox, tech_group):
    """统一邮箱收到含"登录"的邮件时路由到技术支持组。"""
    from apps.routing.models import Rule

    return Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="登录",
        action_type="assign_group",
        action_value=str(tech_group.pk),
    )


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


def test_group_template_used_when_mailbox_routed_to_group(unified_mailbox, tech_group, outbox):
    """统一进线邮箱经规则路由到组后，自动回复必须用该组的覆盖模板。

    回归：组覆盖模板曾按入口邮箱绑定的组解析，而统一/兜底邮箱不绑定组，
    导致路由到组的工单永远只发全局模板。
    """
    from apps.routing.models import Template

    Template.objects.create(scope="global", body="全局模板")
    Template.objects.create(scope="group", group=tech_group, body="技术组专属模板")

    # 入口邮箱未绑定任何组，工单归属组由路由决定
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    assert unified_mailbox.owning_group is None

    assert send_auto_reply(ticket, unified_mailbox) is not None
    message = Message.objects.filter(ticket=ticket, is_auto_reply=True).first()
    assert message.body_text == "技术组专属模板"


def test_routed_group_without_template_falls_back_to_global(
    unified_mailbox, finance_group, outbox
):
    """路由到未配置组模板的组时，回落到全局模板。"""
    from apps.routing.models import Template

    Template.objects.create(scope="global", body="全局模板")

    ticket = make_ticket(mailbox=unified_mailbox, group=finance_group, customer_email=CUSTOMER)
    assert send_auto_reply(ticket, unified_mailbox) is not None
    message = Message.objects.filter(ticket=ticket, is_auto_reply=True).first()
    assert message.body_text == "全局模板"


def test_group_template_ignores_mailbox_group_when_ticket_routed_elsewhere(
    unified_mailbox, tech_group, finance_group, tech_mailbox, outbox
):
    """工单组与邮箱绑定组不一致时，以工单组为准。

    改派后可能出现"入口是组专用邮箱、工单却已在别的组"的情况，
    此时应发工单所属组的模板，而不是邮箱绑定组的模板。
    """
    from apps.routing.models import Template

    Template.objects.create(scope="group", group=tech_group, body="技术组模板")
    Template.objects.create(scope="group", group=finance_group, body="财务组模板")

    ticket = make_ticket(mailbox=tech_mailbox, group=finance_group, customer_email=CUSTOMER)
    assert send_auto_reply(ticket, tech_mailbox) is not None
    message = Message.objects.filter(ticket=ticket, is_auto_reply=True).first()
    assert message.body_text == "财务组模板"


def test_pipeline_routed_ticket_uses_group_template(
    unified_mailbox, tech_group, login_rule, outbox
):
    """端到端：走完整流水线（规则路由 → 自动回复）也必须用组模板。"""
    from apps.routing.models import Template

    Template.objects.create(scope="global", body="全局模板")
    Template.objects.create(scope="group", group=tech_group, body="技术组专属模板")

    result = process_inbound(
        unified_mailbox, 1, build_raw(sender=CUSTOMER, subject="无法登录后台")
    )
    assert result.ticket.group == tech_group
    assert result.auto_replied is True
    message = Message.objects.filter(ticket=result.ticket, is_auto_reply=True).first()
    assert message.body_text == "技术组专属模板"
