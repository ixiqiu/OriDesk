"""工单标签界面层验收用例（视图 / 表单 / 模板）。

覆盖开发文档 §4.4 add_tag 的界面落地：详情页增删标签、收件箱按标签筛选与展示、
标签总览、管理员标签字典维护，以及权限边界与坏参数容错。
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.tickets.models import MAX_TAGS_PER_TICKET, Tag, TicketTag
from tests.conftest import make_ticket

pytestmark = pytest.mark.django_db

CUSTOMER = "customer@customer-domain.com"

MANAGER_PAGES = [
    "routing:tag_admin_list",
    "routing:tag_admin_create",
]


@pytest.fixture
def ticket(unified_mailbox, tech_group):
    return make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)


@pytest.fixture
def manager(admin_group_user):
    """管理员组成员：可维护标签字典。"""
    return admin_group_user


# ------------------------------------------------------------------ 访问控制
def test_anonymous_is_redirected_to_login(client, ticket):
    login = reverse("accounts:login")
    response = client.get(reverse("tickets:tag_list"))
    assert response.status_code == 302
    assert login in response["Location"]

    response = client.post(reverse("tickets:tag_add", args=[ticket.pk]), {"new_tag": "合同"})
    assert response.status_code == 302
    assert login in response["Location"]


@pytest.mark.parametrize("url_name", ["routing:tag_admin_list", "routing:tag_admin_create"])
def test_anonymous_redirected_from_tag_admin(client, url_name):
    response = client.get(reverse(url_name))
    assert response.status_code == 302
    assert reverse("accounts:login") in response["Location"]


def test_other_group_user_gets_404_on_tag_add(client, finance_user, ticket):
    """不可见工单：打标签返回 404（不泄露存在性）。"""
    client.force_login(finance_user)
    response = client.post(
        reverse("tickets:tag_add", args=[ticket.pk]), {"new_tag": "越权标签"}
    )
    assert response.status_code == 404
    assert TicketTag.objects.filter(ticket=ticket).count() == 0


def test_other_group_user_gets_404_on_tag_remove(client, finance_user, ticket):
    tag = Tag.objects.create(name="紧急")
    TicketTag.objects.create(ticket=ticket, tag=tag)
    client.force_login(finance_user)
    response = client.post(reverse("tickets:tag_remove", args=[ticket.pk, tag.pk]))
    assert response.status_code == 404
    assert TicketTag.objects.filter(ticket=ticket, tag=tag).count() == 1


# ------------------------------------------------------------------ 详情页打 / 移标签
def test_add_tag_via_existing_dropdown(client, tech_user, ticket):
    tag = Tag.objects.create(name="紧急", color="red")
    client.force_login(tech_user)

    response = client.post(reverse("tickets:tag_add", args=[ticket.pk]), {"tag": tag.pk})

    assert response.status_code == 302
    link = TicketTag.objects.get(ticket=ticket, tag=tag)
    assert link.added_by == tech_user
    assert link.source == "manual"


def test_add_tag_via_new_name_creates_group_scoped_tag(client, tech_user, ticket, tech_group):
    client.force_login(tech_user)

    response = client.post(
        reverse("tickets:tag_add", args=[ticket.pk]), {"new_tag": "  合同   审核  "}
    )

    assert response.status_code == 302
    tag = Tag.objects.get(name="合同 审核")  # 名称已归一化
    assert tag.group_id == tech_group.pk
    assert TicketTag.objects.filter(ticket=ticket, tag=tag).count() == 1


def test_add_same_tag_twice_keeps_single_link(client, tech_user, ticket):
    client.force_login(tech_user)
    url = reverse("tickets:tag_add", args=[ticket.pk])

    client.post(url, {"new_tag": "合同"})
    response = client.post(url, {"new_tag": "合同"})

    assert response.status_code == 302
    assert Tag.objects.filter(name="合同").count() == 1
    assert TicketTag.objects.filter(ticket=ticket).count() == 1


def test_blank_tag_name_is_rejected_without_persisting(client, tech_user, ticket):
    client.force_login(tech_user)

    response = client.post(
        reverse("tickets:tag_add", args=[ticket.pk]), {"new_tag": "   "}, follow=True
    )

    assert response.status_code == 200
    assert TicketTag.objects.filter(ticket=ticket).count() == 0
    assert Tag.objects.count() == 0
    assert "标签名不能为空" in response.content.decode()  # 面向用户的错误提示


def test_empty_form_is_rejected(client, tech_user, ticket):
    client.force_login(tech_user)
    response = client.post(
        reverse("tickets:tag_add", args=[ticket.pk]), {"tag": "", "new_tag": ""}, follow=True
    )
    assert response.status_code == 200
    assert TicketTag.objects.filter(ticket=ticket).count() == 0
    assert "请选择已有标签" in response.content.decode()


def test_cannot_add_other_group_tag_by_forging_id(client, tech_user, ticket, finance_group):
    """下拉只列出全局 + 本组标签；伪造他组标签 ID 会被表单拒绝。"""
    foreign = Tag.objects.create(name="财务专属", group=finance_group)
    client.force_login(tech_user)

    response = client.post(reverse("tickets:tag_add", args=[ticket.pk]), {"tag": foreign.pk})

    assert response.status_code == 302
    assert TicketTag.objects.filter(ticket=ticket, tag=foreign).count() == 0


def test_disabled_tag_name_is_rejected(client, tech_user, ticket):
    Tag.objects.create(name="已停用标签", is_active=False)
    client.force_login(tech_user)

    response = client.post(
        reverse("tickets:tag_add", args=[ticket.pk]), {"new_tag": "已停用标签"}, follow=True
    )

    assert response.status_code == 200
    assert TicketTag.objects.filter(ticket=ticket).count() == 0


def test_max_tags_per_ticket_is_reported_not_500(client, tech_user, ticket):
    tags = [Tag.objects.create(name=f"标签{i}") for i in range(MAX_TAGS_PER_TICKET)]
    TicketTag.objects.bulk_create([TicketTag(ticket=ticket, tag=tag) for tag in tags])
    client.force_login(tech_user)

    response = client.post(
        reverse("tickets:tag_add", args=[ticket.pk]), {"new_tag": "溢出标签"}, follow=True
    )

    assert response.status_code == 200
    assert TicketTag.objects.filter(ticket=ticket).count() == MAX_TAGS_PER_TICKET
    assert "最多" in response.content.decode()


def test_remove_tag(client, tech_user, ticket):
    tag = Tag.objects.create(name="合同")
    TicketTag.objects.create(ticket=ticket, tag=tag)
    client.force_login(tech_user)

    response = client.post(reverse("tickets:tag_remove", args=[ticket.pk, tag.pk]))

    assert response.status_code == 302
    assert TicketTag.objects.filter(ticket=ticket, tag=tag).count() == 0
    assert Tag.objects.filter(pk=tag.pk).exists()  # 移除关联不删除标签字典


def test_htmx_tag_add_returns_timeline_partial(client, tech_user, ticket):
    client.force_login(tech_user)
    response = client.post(
        reverse("tickets:tag_add", args=[ticket.pk]),
        {"new_tag": "合同"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "tickets/_timeline.html" in [template.name for template in response.templates]


# ------------------------------------------------------------------ 详情页展示
def test_detail_shows_tags_and_foreign_marker(
    client, tech_user, ticket, unified_mailbox, finance_group
):
    own = Tag.objects.create(name="本组标签", color="green", group=ticket.group)
    foreign = Tag.objects.create(name="历史标签", color="amber", group=finance_group)
    available = Tag.objects.create(name="可选全局标签", color="blue")
    TicketTag.objects.create(ticket=ticket, tag=own)
    TicketTag.objects.create(ticket=ticket, tag=foreign)
    client.force_login(tech_user)

    content = client.get(reverse("tickets:detail", args=[ticket.pk])).content.decode()

    assert "本组标签" in content
    assert "tag-green" in content
    assert "历史标签" in content
    assert "历史标签（原属" in content  # is_foreign 的弱化提示
    assert reverse("tickets:tag_remove", args=[ticket.pk, own.pk]) in content
    # 「添加标签」下拉列出可用标签（全局标签在列）
    assert f'value="{available.pk}"' in content


# ------------------------------------------------------------------ 收件箱筛选 / 展示
def test_inbox_filter_by_tag_and_bad_value(
    client, tech_user, unified_mailbox, tech_group
):
    tagged = make_ticket(mailbox=unified_mailbox, group=tech_group, subject="带标签工单")
    other = make_ticket(mailbox=unified_mailbox, group=tech_group, subject="无标签工单")
    tag = Tag.objects.create(name="紧急")
    TicketTag.objects.create(ticket=tagged, tag=tag)
    client.force_login(tech_user)

    response = client.get(reverse("tickets:inbox"), {"tag": tag.pk})
    content = response.content.decode()
    assert response.status_code == 200
    assert f"T#{tagged.pk}" in content
    assert f"T#{other.pk}" not in content

    # 非法值不能 500，且退回未筛选结果
    response = client.get(reverse("tickets:inbox"), {"tag": "abc"})
    assert response.status_code == 200
    assert f"T#{tagged.pk}" in response.content.decode()


def test_inbox_filter_respects_visibility(client, tech_user, unified_mailbox, finance_group):
    hidden = make_ticket(mailbox=unified_mailbox, group=finance_group, subject="他组工单")
    tag = Tag.objects.create(name="共享标签")
    TicketTag.objects.create(ticket=hidden, tag=tag)
    client.force_login(tech_user)

    response = client.get(reverse("tickets:inbox"), {"tag": tag.pk})
    assert response.status_code == 200
    assert f"T#{hidden.pk}" not in response.content.decode()


def test_inbox_row_shows_tags_with_overflow(client, tech_user, ticket):
    tags = [Tag.objects.create(name=f"标签{i}") for i in range(4)]
    TicketTag.objects.bulk_create([TicketTag(ticket=ticket, tag=tag) for tag in tags])
    client.force_login(tech_user)

    content = client.get(reverse("tickets:inbox")).content.decode()
    assert "tag-badge" in content
    assert "…+1" in content  # 超过 3 个折叠


# ------------------------------------------------------------------ 标签总览页
def test_tag_list_is_visible_to_any_logged_in_user(client, tech_user, tech_group):
    Tag.objects.create(name="全局标签")
    Tag.objects.create(name="组标签", group=tech_group)
    client.force_login(tech_user)

    response = client.get(reverse("tickets:tag_list"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "全局标签" in content
    assert "组标签" in content
    assert reverse("routing:tag_admin_list") not in content  # 非管理员无「去管理」


def test_tag_list_shows_manage_link_for_manager(client, manager):
    client.force_login(manager)
    content = client.get(reverse("tickets:tag_list")).content.decode()
    assert reverse("routing:tag_admin_list") in content


# ------------------------------------------------------------------ 标签字典管理
@pytest.mark.parametrize("url_name", MANAGER_PAGES)
def test_normal_user_forbidden_on_tag_admin(client, tech_user, url_name):
    client.force_login(tech_user)
    assert client.get(reverse(url_name)).status_code == 403


def test_normal_user_forbidden_on_tag_admin_writes(client, tech_user, ticket):
    tag = Tag.objects.create(name="合同")
    client.force_login(tech_user)
    assert client.post(reverse("routing:tag_admin_toggle", args=[tag.pk])).status_code == 403
    assert client.post(reverse("routing:tag_admin_delete", args=[tag.pk])).status_code == 403
    assert client.post(
        reverse("routing:tag_admin_create"), {"name": "越权", "color": "slate"}
    ).status_code == 403


def test_manager_can_create_edit_toggle_delete(client, manager, tech_group):
    client.force_login(manager)

    assert client.get(reverse("routing:tag_admin_list")).status_code == 200

    response = client.post(
        reverse("routing:tag_admin_create"),
        {"name": "  合同  ", "group": tech_group.pk, "color": "blue", "description": "合同类", "is_active": "on"},
    )
    assert response.status_code == 302
    tag = Tag.objects.get(name="合同")
    assert tag.group_id == tech_group.pk
    assert tag.color == "blue"

    response = client.post(
        reverse("routing:tag_admin_edit", args=[tag.pk]),
        {"name": "合同审核", "group": tech_group.pk, "color": "red", "description": "", "is_active": "on"},
    )
    assert response.status_code == 302
    tag.refresh_from_db()
    assert tag.name == "合同审核"
    assert tag.color == "red"

    client.post(reverse("routing:tag_admin_toggle", args=[tag.pk]))
    tag.refresh_from_db()
    assert tag.is_active is False

    client.post(reverse("routing:tag_admin_delete", args=[tag.pk]))
    assert not Tag.objects.filter(pk=tag.pk).exists()


def test_tag_admin_form_pages_render(client, manager):
    tag = Tag.objects.create(name="合同", color="blue")
    client.force_login(manager)

    response = client.get(reverse("routing:tag_admin_create"))
    assert response.status_code == 200
    assert "name" in response.context["form"].fields

    response = client.get(reverse("routing:tag_admin_edit", args=[tag.pk]))
    assert response.status_code == 200
    assert response.context["tag"].pk == tag.pk
    assert response.context["editing"] is True


def test_tag_admin_edit_duplicate_name_shows_form_error(client, manager, tech_group):
    Tag.objects.create(name="紧急")
    Tag.objects.create(name="发票", group=tech_group)
    client.force_login(manager)

    # 全局同名
    response = client.post(
        reverse("routing:tag_admin_create"), {"name": "紧急", "color": "slate"}
    )
    assert response.status_code == 200
    assert "name" in response.context["form"].errors
    assert Tag.objects.filter(name="紧急").count() == 1

    # 组作用域同名
    response = client.post(
        reverse("routing:tag_admin_create"),
        {"name": "发票", "group": tech_group.pk, "color": "slate"},
    )
    assert response.status_code == 200
    assert "name" in response.context["form"].errors
    assert Tag.objects.filter(name="发票", group=tech_group).count() == 1


def test_tag_admin_list_marks_disabled_and_counts_usage(client, manager, ticket):
    active = Tag.objects.create(name="启用标签")
    Tag.objects.create(name="停用标签", is_active=False)
    TicketTag.objects.create(ticket=ticket, tag=active)
    client.force_login(manager)

    content = client.get(reverse("routing:tag_admin_list")).content.decode()

    assert "启用标签" in content
    assert "停用标签" in content
    assert "已停用" in content
    assert "级联" in content  # 删除风险提示


def test_delete_tag_cascades_ticket_links(client, manager, ticket):
    tag = Tag.objects.create(name="合同")
    link = TicketTag.objects.create(ticket=ticket, tag=tag)
    client.force_login(manager)

    response = client.post(reverse("routing:tag_admin_delete", args=[tag.pk]), follow=True)

    assert response.status_code == 200
    assert TicketTag.objects.filter(pk=link.pk).exists() is False
    assert TicketTag.objects.filter(ticket=ticket).count() == 0
    assert not Tag.objects.filter(pk=tag.pk).exists()
