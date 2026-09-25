"""autoresponder 管理界面视图测试（开发文档 §2.4 / §5.3 / §12.3）。

覆盖：权限边界、全局模板创建与更新、组覆盖模板创建与删除、预览渲染、模型约束校验。
fixture 在本文件内自建，避免依赖其它测试包的 conftest。
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import Group, Mailbox, User, UserGroup
from apps.autoresponder.services import DEFAULT_TEMPLATE, resolve_template_body
from apps.routing.models import Template

pytestmark = pytest.mark.django_db

PASSWORD = "DemoPass!2345"


@pytest.fixture(autouse=True)
def _clear_setting_cache():
    cache.clear()
    yield
    cache.clear()


# ------------------------------------------------------------------ 工具
def make_mailbox(email="support@example.com", name="兜底邮箱", *, is_fallback=True):
    mailbox = Mailbox(
        name=name,
        email=email,
        imap_host="imap.example.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_ssl=True,
        username=email,
        secret_encrypted=b"",
        is_fallback=is_fallback,
        is_active=True,
    )
    mailbox.set_secret("demo-auth-code")
    mailbox.save()
    return mailbox


def make_user(username, *, is_superadmin=False, group=None, is_admin=False):
    user = User.objects.create_user(
        username=username, password=PASSWORD, is_superadmin=is_superadmin
    )
    if group is not None:
        UserGroup.objects.create(user=user, group=group, is_admin=is_admin)
    return user


# ------------------------------------------------------------------ fixture
@pytest.fixture
def tech_group(db):
    return Group.objects.create(name="技术支持组")


@pytest.fixture
def finance_group(db):
    return Group.objects.create(name="财务组")


@pytest.fixture
def admin_group(db):
    return Group.objects.create(name="管理员组", is_admin_group=True)


@pytest.fixture
def manager(db, admin_group):
    return make_user("manager", group=admin_group, is_admin=True)


@pytest.fixture
def staff(db, tech_group):
    return make_user("staff", group=tech_group)


@pytest.fixture
def fallback_mailbox(db):
    return make_mailbox()


# ------------------------------------------------------------------ 权限边界
def test_anonymous_is_redirected_to_login(client):
    response = client.get(reverse("autoresponder:template_list"))
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.parametrize(
    "url_name,args",
    [
        ("autoresponder:template_list", []),
        ("autoresponder:template_edit", []),
        ("autoresponder:group_template_edit", [1]),
    ],
)
def test_normal_user_forbidden(client, staff, tech_group, url_name, args):
    if url_name == "autoresponder:group_template_edit":
        args = [tech_group.pk]
    client.force_login(staff)
    assert client.get(reverse(url_name, args=args)).status_code == 403


def test_manager_can_open_pages(client, manager, tech_group):
    client.force_login(manager)
    assert client.get(reverse("autoresponder:template_list")).status_code == 200
    assert client.get(reverse("autoresponder:template_edit")).status_code == 200
    assert (
        client.get(reverse("autoresponder:group_template_edit", args=[tech_group.pk])).status_code
        == 200
    )


def test_group_template_edit_unknown_group_is_404(client, manager):
    client.force_login(manager)
    assert (
        client.get(reverse("autoresponder:group_template_edit", args=[999999])).status_code == 404
    )


# ------------------------------------------------------------------ 全局模板
def test_template_list_shows_defaults_when_empty(client, manager, tech_group):
    client.force_login(manager)
    response = client.get(reverse("autoresponder:template_list"))
    assert response.status_code == 200
    assert response.context["global_template"] is None
    assert response.context["variables"]
    content = response.content.decode()
    assert "使用全局模板" in content
    assert "ticket_id" in content


def test_global_template_create_then_update(client, manager):
    client.force_login(manager)

    response = client.post(
        reverse("autoresponder:template_edit"),
        {"body": "您好，工单 {{ ticket_no }} 已受理。", "_save": "1"},
    )
    assert response.status_code == 302
    template = Template.objects.get(scope="global")
    assert "已受理" in template.body
    assert template.group_id is None

    response = client.post(
        reverse("autoresponder:template_edit"),
        {"body": "更新后的全局模板 {{ ticket_no }}", "_save": "1"},
    )
    assert response.status_code == 302
    assert Template.objects.filter(scope="global").count() == 1
    assert Template.objects.get(scope="global").body == "更新后的全局模板 {{ ticket_no }}"


def test_global_template_preview_does_not_save(client, manager):
    client.force_login(manager)
    body = "工单 {{ ticket_no }}，客户 {{ customer_email }}，组 {{ group_name }}，"
    body += "身份 {{ identity_email }}，时间 {{ date }}，编号 {{ ticket_id }}，主题 {{ subject }}"
    response = client.post(
        reverse("autoresponder:template_edit"),
        {"body": body, "_preview": "1"},
    )
    assert response.status_code == 200
    assert Template.objects.count() == 0
    preview = response.context["preview"]
    assert "[T#1234]" in preview
    assert "customer@example.com" in preview
    assert "示例用户组" in preview
    assert "support@example.com" in preview
    assert "1234" in preview
    assert "示例：无法登录后台" in preview


def test_global_template_rejects_empty_body(client, manager):
    client.force_login(manager)
    response = client.post(
        reverse("autoresponder:template_edit"), {"body": "   ", "_save": "1"}
    )
    assert response.status_code == 200
    assert "body" in response.context["form"].errors
    assert Template.objects.count() == 0


# ------------------------------------------------------------------ 组覆盖模板
def test_group_template_create_and_delete_falls_back_to_global(
    client, manager, tech_group, fallback_mailbox
):
    Template.objects.create(scope="global", body="全局模板 {# 默认 #}")
    client.force_login(manager)

    response = client.post(
        reverse("autoresponder:group_template_edit", args=[tech_group.pk]),
        {"body": "技术组专属模板 {{ ticket_no }}", "_save": "1"},
    )
    assert response.status_code == 302
    template = Template.objects.get(scope="group", group=tech_group)
    assert template.body.startswith("技术组专属")

    # 组邮箱绑定后，解析应优先命中组覆盖模板
    group_mailbox = make_mailbox(email="tech@example.com", name="技术支持组邮箱", is_fallback=False)
    tech_group.mailbox = group_mailbox
    tech_group.save(update_fields=["mailbox"])
    assert resolve_template_body(group_mailbox) == template.body

    response = client.post(
        reverse("autoresponder:group_template_delete", args=[tech_group.pk]), follow=True
    )
    assert response.status_code == 200
    assert not Template.objects.filter(scope="group", group=tech_group).exists()
    # 删除后回落到全局模板
    assert resolve_template_body(group_mailbox) == "全局模板 {# 默认 #}"
    assert "回落到全局模板" in response.content.decode()


def test_group_template_delete_without_template_reports_message(client, manager, tech_group):
    client.force_login(manager)
    response = client.post(
        reverse("autoresponder:group_template_delete", args=[tech_group.pk]), follow=True
    )
    assert response.status_code == 200
    assert "没有专属模板" in response.content.decode()


def test_template_delete_requires_post(client, manager, tech_group):
    Template.objects.create(scope="group", group=tech_group, body="专属")
    client.force_login(manager)
    assert (
        client.get(reverse("autoresponder:group_template_delete", args=[tech_group.pk])).status_code
        == 405
    )
    assert Template.objects.filter(scope="group", group=tech_group).exists()


def test_group_template_edit_shows_group_identity_in_preview(client, manager, tech_group, fallback_mailbox):
    client.force_login(manager)
    response = client.get(reverse("autoresponder:group_template_edit", args=[tech_group.pk]))
    assert response.status_code == 200
    assert response.context["group"].pk == tech_group.pk
    assert response.context["title"] == f"「{tech_group.name}」专属自动回复模板"
    # 无专属模板时，编辑框预填内置默认模板
    assert response.context["default_template"] == DEFAULT_TEMPLATE
    assert tech_group.name in response.context["preview"]

    # 组身份（Group.identity_email）进入预览上下文
    response = client.post(
        reverse("autoresponder:group_template_edit", args=[tech_group.pk]),
        {"body": "本组对外身份 {{ identity_email }}", "_preview": "1"},
    )
    assert response.status_code == 200
    assert tech_group.identity_email in response.context["preview"]
