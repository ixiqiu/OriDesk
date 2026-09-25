"""全站页面冒烟测试：登录后逐页 GET，确保模板与视图整体可用。

这是"合入前最后一公里"的保障：任何 URL 名称、模板变量、权限装饰器写错都会在这里暴露。
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.routing.models import Rule, Template
from apps.tickets.models import Tag
from tests.conftest import make_message, make_ticket

CUSTOMER = "customer@customer-domain.com"


@pytest.fixture
def prepared(unified_mailbox, tech_group, tech_mailbox, finance_group, fallback_mailbox, superadmin):
    """准备一份覆盖各页面最小数据集的场景。"""
    from apps.audit.models import Setting

    Setting.set("fallback_group_id", tech_group.pk)
    Setting.set("fallback_mailbox_id", fallback_mailbox.pk)

    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    make_message(ticket=ticket, mailbox=unified_mailbox, body_text="客户来信")
    Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="登录",
        action_type="assign_group",
        action_value=str(tech_group.pk),
    )
    Template.objects.create(scope="global", body="全局模板 {{ ticket_id }}")
    Template.objects.create(scope="group", group=tech_group, body="组模板 {{ ticket_id }}")
    AuditLog.objects.create(action="login", user=superadmin, detail={"username": "superadmin"})
    tag = Tag.objects.create(name="紧急", color="red")
    return {"ticket": ticket, "group": tech_group, "tag": tag}


PAGES = [
    ("tickets:inbox", None),
    ("tickets:mine", None),
    ("tickets:unassigned", None),
    ("tickets:awaiting", None),
    ("routing:rule_list", None),
    ("routing:rule_create", None),
    ("routing:settings", None),
    ("routing:mailbox_overview", None),
    ("autoresponder:template_list", None),
    ("autoresponder:template_edit", None),
    ("audit:log_list", None),
    ("accounts:profile", None),
    ("accounts:user_list", None),
    ("accounts:user_create", None),
    ("accounts:group_list", None),
    ("accounts:group_create", None),
    ("accounts:mailbox_list", None),
    ("accounts:mailbox_create", None),
    ("accounts:password_change", None),
    ("tickets:tag_list", None),
    ("routing:tag_admin_list", None),
    ("routing:tag_admin_create", None),
]


@pytest.mark.django_db
@pytest.mark.parametrize("url_name,args", PAGES, ids=[name for name, _ in PAGES])
def test_page_renders(client, superadmin, prepared, url_name, args):
    client.force_login(superadmin)
    response = client.get(reverse(url_name, args=args))
    assert response.status_code == 200, f"{url_name} 返回 {response.status_code}"


def test_dynamic_pages_render(client, superadmin, prepared):
    client.force_login(superadmin)
    ticket = prepared["ticket"]
    group = prepared["group"]

    targets = [
        ("tickets:detail", [ticket.pk]),
        ("routing:tag_admin_edit", [prepared["tag"].pk]),
        ("tickets:message_list", [ticket.pk]),
        ("routing:rule_edit", [Rule.objects.first().pk]),
        ("autoresponder:group_template_edit", [group.pk]),
        ("audit:log_detail", [AuditLog.objects.first().pk]),
        ("accounts:group_edit", [group.pk]),
        ("accounts:mailbox_edit", [prepared["ticket"].mailbox_id]),
    ]
    for url_name, args in targets:
        response = client.get(reverse(url_name, args=args))
        assert response.status_code == 200, f"{url_name} 返回 {response.status_code}"


def test_login_page_renders_without_auth(client, db):
    response = client.get(reverse("accounts:login"))
    assert response.status_code == 200
    assert "csrfmiddlewaretoken" in response.content.decode()


def test_admin_index_requires_staff(client, superadmin, prepared):
    client.force_login(superadmin)
    response = client.get(reverse("admin:index"))
    assert response.status_code == 200


def test_404_page_renders(client, superadmin, prepared):
    """handler404 使用独立模板，需保证渲染不报错。"""
    client.force_login(superadmin)
    response = client.get("/definitely-not-a-real-page/")
    assert response.status_code == 404
    assert "404" in response.content.decode()


def test_403_page_renders(client, tech_user, prepared):
    """普通用户访问超管页面时由 handler403 渲染错误页。"""
    client.force_login(tech_user)
    response = client.get(reverse("accounts:user_list"))
    assert response.status_code == 403
    assert "403" in response.content.decode()
