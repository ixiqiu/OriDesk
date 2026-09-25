"""routing 管理界面视图测试（开发文档 §12.2 / §10.3）。

覆盖：权限边界、规则增删启停、规则试算（只读）、系统设置写入与审计、邮箱总览手动同步。
fixture 在本文件内自建，避免依赖其它测试包的 conftest。
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import Group, Mailbox, User, UserGroup
from apps.audit.models import AuditLog, Setting
from apps.routing.models import Rule
from apps.tickets.models import Message, Ticket

pytestmark = pytest.mark.django_db

PASSWORD = "DemoPass!2345"

MANAGER_PAGES = ["routing:rule_list", "routing:settings", "routing:mailbox_overview"]


@pytest.fixture(autouse=True)
def _clear_setting_cache():
    """Setting.get 走 Django cache，测试之间必须清干净。"""
    cache.clear()
    yield
    cache.clear()


# ------------------------------------------------------------------ 工具
def make_mailbox(email="support@example.com", name=None, *, is_fallback=False, is_active=True):
    mailbox = Mailbox(
        name=name or email.split("@")[0],
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
        is_active=is_active,
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


def make_rule(mailbox, target_group, *, priority=100, match_value="发票", enabled=True):
    return Rule.objects.create(
        mailbox=mailbox,
        priority=priority,
        enabled=enabled,
        match_field="subject",
        match_op="contains",
        match_value=match_value,
        action_type="assign_group",
        action_value=str(target_group.pk),
    )


# ------------------------------------------------------------------ fixture
@pytest.fixture
def unified_mailbox(db):
    return make_mailbox()


@pytest.fixture
def fallback_mailbox(db):
    return make_mailbox(email="fallback@example.com", name="全局兜底邮箱", is_fallback=True)


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
    """管理员组成员：可维护路由 / 设置 / 邮箱总览。"""
    return make_user("manager", group=admin_group, is_admin=True)


@pytest.fixture
def staff(db, tech_group):
    """普通用户：无管理权限。"""
    return make_user("staff", group=tech_group)


# ------------------------------------------------------------------ 权限边界
def test_anonymous_is_redirected_to_login(client):
    response = client.get(reverse("routing:rule_list"))
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.parametrize("url_name", MANAGER_PAGES)
def test_normal_user_forbidden(client, staff, url_name):
    client.force_login(staff)
    assert client.get(reverse(url_name)).status_code == 403


@pytest.mark.parametrize("url_name", MANAGER_PAGES)
def test_manager_can_open_pages(client, manager, url_name):
    client.force_login(manager)
    assert client.get(reverse(url_name)).status_code == 200


def test_rule_form_pages_render(client, manager, unified_mailbox, tech_group):
    rule = make_rule(unified_mailbox, tech_group)
    client.force_login(manager)
    assert client.get(reverse("routing:rule_create")).status_code == 200
    response = client.get(reverse("routing:rule_edit", args=[rule.pk]))
    assert response.status_code == 200
    assert response.context["rule"].pk == rule.pk


# ------------------------------------------------------------------ 规则启停 / 删除
def test_rule_toggle_flips_enabled(client, manager, unified_mailbox, tech_group):
    rule = make_rule(unified_mailbox, tech_group)
    client.force_login(manager)

    response = client.post(reverse("routing:rule_toggle", args=[rule.pk]))
    assert response.status_code == 302
    rule.refresh_from_db()
    assert rule.enabled is False

    client.post(reverse("routing:rule_toggle", args=[rule.pk]))
    rule.refresh_from_db()
    assert rule.enabled is True


def test_rule_toggle_requires_post(client, manager, unified_mailbox, tech_group):
    rule = make_rule(unified_mailbox, tech_group)
    client.force_login(manager)
    assert client.get(reverse("routing:rule_toggle", args=[rule.pk])).status_code == 405
    assert Rule.objects.filter(pk=rule.pk).exists()


def test_rule_delete(client, manager, unified_mailbox, tech_group):
    rule = make_rule(unified_mailbox, tech_group)
    client.force_login(manager)

    response = client.post(reverse("routing:rule_delete", args=[rule.pk]), follow=True)
    assert response.status_code == 200
    assert not Rule.objects.filter(pk=rule.pk).exists()
    assert "已删除" in response.content.decode()


def test_rule_create_uses_group_dropdown(client, manager, unified_mailbox, tech_group):
    client.force_login(manager)
    response = client.post(
        reverse("routing:rule_create"),
        {
            "mailbox": unified_mailbox.pk,
            "priority": 5,
            "enabled": "on",
            "match_field": "subject",
            "match_op": "contains",
            "match_value": "报修",
            "action_type": "assign_group",
            "action_value": "",
            "action_group": tech_group.pk,
        },
    )
    assert response.status_code == 302
    rule = Rule.objects.get()
    assert rule.action_value == str(tech_group.pk)
    assert rule.priority == 5


def test_rule_create_rejects_bad_regex(client, manager, unified_mailbox, tech_group):
    client.force_login(manager)
    response = client.post(
        reverse("routing:rule_create"),
        {
            "mailbox": unified_mailbox.pk,
            "priority": 5,
            "enabled": "on",
            "match_field": "subject",
            "match_op": "regex",
            "match_value": "([",
            "action_type": "assign_group",
            "action_group": tech_group.pk,
        },
    )
    assert response.status_code == 200
    assert "match_value" in response.context["form"].errors
    assert Rule.objects.count() == 0


def test_rule_edit_updates_rule(client, manager, unified_mailbox, tech_group, finance_group):
    rule = make_rule(unified_mailbox, tech_group)
    client.force_login(manager)
    response = client.post(
        reverse("routing:rule_edit", args=[rule.pk]),
        {
            "mailbox": unified_mailbox.pk,
            "priority": 1,
            "match_field": "from",
            "match_op": "domain",
            "match_value": "customer-domain.com",
            "action_type": "assign_group",
            "action_group": finance_group.pk,
        },
    )
    assert response.status_code == 302
    rule.refresh_from_db()
    assert rule.priority == 1
    assert rule.action_value == str(finance_group.pk)
    assert rule.enabled is False  # 复选框未勾选即为停用


# ------------------------------------------------------------------ 规则试算
def test_rule_trial_hits_expected_rule_without_side_effects(
    client, manager, unified_mailbox, tech_group, finance_group
):
    winning = make_rule(unified_mailbox, finance_group, priority=10)
    make_rule(unified_mailbox, tech_group, priority=20)
    tickets_before = Ticket.objects.count()
    client.force_login(manager)

    response = client.post(
        reverse("routing:rule_list"),
        {
            "mailbox": unified_mailbox.pk,
            "subject": "[紧急] 发票系统故障",
            "sender": "customer@customer-domain.com",
            "to": unified_mailbox.email,
            "body": "麻烦尽快处理",
        },
    )

    assert response.status_code == 200
    result = response.context["trial_result"]
    assert result is not None
    assert result["matched_rule"].pk == winning.pk
    assert result["group"].pk == finance_group.pk
    assert result["error"] == ""
    # 试算不写任何数据
    assert Ticket.objects.count() == tickets_before
    assert Message.objects.count() == 0


def test_rule_trial_considers_sticky_ticket(
    client, manager, unified_mailbox, tech_group, finance_group
):
    """粘性优先于规则：同发件人窗口内已有工单时归原组。"""
    make_rule(unified_mailbox, finance_group, priority=10, match_value="发票")
    from django.utils import timezone

    Ticket.objects.create(
        mailbox=unified_mailbox,
        group=tech_group,
        subject="历史工单",
        normalized_subject="历史工单",
        customer_email="customer@customer-domain.com",
        last_message_at=timezone.now(),
    )
    client.force_login(manager)

    response = client.post(
        reverse("routing:rule_list"),
        {
            "mailbox": unified_mailbox.pk,
            "subject": "发票系统故障",
            "sender": "customer@customer-domain.com",
        },
    )
    result = response.context["trial_result"]
    assert result["matched_rule"] is not None
    assert result["sticky_ticket"] is not None
    assert result["group"].pk == tech_group.pk  # 粘性覆盖规则


def test_rule_trial_ignores_group_mailbox_rules(client, manager, tech_group):
    """组专用邮箱直接进组，规则不参与（§2.2）。"""
    group_mailbox = make_mailbox(email="tech@example.com", name="技术支持组邮箱")
    tech_group.mailbox = group_mailbox
    tech_group.save(update_fields=["mailbox"])
    other_group = Group.objects.create(name="另一个组")
    make_rule(group_mailbox, other_group, priority=1)

    client.force_login(manager)
    response = client.post(
        reverse("routing:rule_list"),
        {"mailbox": group_mailbox.pk, "subject": "发票"},
    )
    result = response.context["trial_result"]
    assert result["kind"] == "group"
    assert result["group"].pk == tech_group.pk


# ------------------------------------------------------------------ 系统设置
def test_settings_save_persists_and_writes_audit(client, manager):
    client.force_login(manager)
    response = client.post(
        reverse("routing:settings"),
        {
            "sticky_window_days": 14,
            "first_contact_window_hours": 48,
            "fallback_group_id": "",
            "fallback_mailbox_id": "",
            "max_attachment_size_mb": 30,
            "imap_poll_interval_seconds": 120,
        },
    )
    assert response.status_code == 302
    assert Setting.get("sticky_window_days") == "14"
    assert Setting.objects.get(key="sticky_window_days").value == "14"

    log = AuditLog.objects.get(action="config_change", detail__key="sticky_window_days")
    assert log.user_id == manager.pk
    assert log.detail["value"] == "14"


def test_settings_save_fallback_objects(client, manager, tech_group, unified_mailbox):
    client.force_login(manager)
    response = client.post(
        reverse("routing:settings"),
        {
            "sticky_window_days": 7,
            "first_contact_window_hours": 24,
            "fallback_group_id": tech_group.pk,
            "fallback_mailbox_id": unified_mailbox.pk,
            "max_attachment_size_mb": 25,
            "imap_poll_interval_seconds": 60,
        },
    )
    assert response.status_code == 302
    assert Setting.get_optional_int("fallback_group_id") == tech_group.pk
    assert Setting.get_optional_int("fallback_mailbox_id") == unified_mailbox.pk
    assert AuditLog.objects.filter(action="config_change").count() == 6


def test_settings_rejects_out_of_range_values(client, manager):
    client.force_login(manager)
    response = client.post(
        reverse("routing:settings"),
        {
            "sticky_window_days": 0,
            "first_contact_window_hours": 24,
            "fallback_group_id": "",
            "fallback_mailbox_id": "",
            "max_attachment_size_mb": 999,
            "imap_poll_interval_seconds": 3,
        },
    )
    assert response.status_code == 200
    errors = response.context["form"].errors
    assert set(errors) == {
        "sticky_window_days",
        "max_attachment_size_mb",
        "imap_poll_interval_seconds",
    }
    assert AuditLog.objects.filter(action="config_change").count() == 0


def test_settings_page_shows_unconfigured_fallback_warning(client, manager):
    client.force_login(manager)
    response = client.get(reverse("routing:settings"))
    assert response.status_code == 200
    assert response.context["fallback_group"] is None
    assert response.context["fallback_mailbox"] is None
    content = response.content.decode()
    assert "未配置" in content


# ------------------------------------------------------------------ 邮箱总览
def test_mailbox_overview_lists_kinds_and_sync_state(
    client, manager, unified_mailbox, tech_group, fallback_mailbox
):
    group_mailbox = make_mailbox(email="tech@example.com", name="技术支持组邮箱")
    tech_group.mailbox = group_mailbox
    tech_group.save(update_fields=["mailbox"])
    make_rule(unified_mailbox, tech_group)

    client.force_login(manager)
    response = client.get(reverse("routing:mailbox_overview"))
    assert response.status_code == 200
    rows = {row["mailbox"].pk: row for row in response.context["rows"]}
    assert rows[unified_mailbox.pk]["kind"] == "unified"
    assert rows[fallback_mailbox.pk]["kind"] == "fallback"
    assert rows[group_mailbox.pk]["kind"] == "group"
    assert rows[group_mailbox.pk]["identity_email"] == group_mailbox.email
    assert rows[unified_mailbox.pk]["rule_count"] == 1
    content = response.content.decode()
    assert "last_uid" in content and "uidvalidity" in content


def test_mailbox_sync_dispatches_and_reports(client, manager, unified_mailbox, monkeypatch):
    calls = []

    def fake_dispatch(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return "inline", {
            "mailbox": unified_mailbox.email,
            "fetched": 3,
            "processed": 2,
            "skipped": 1,
            "failed": 0,
        }

    monkeypatch.setattr("apps.routing.views.dispatch", fake_dispatch)
    client.force_login(manager)

    response = client.post(
        reverse("routing:mailbox_overview"),
        {"mailbox_id": unified_mailbox.pk},
        follow=True,
    )
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][0].__name__ == "sync_mailbox_task"
    assert calls[0][1] == (unified_mailbox.pk,)
    content = response.content.decode()
    assert "入库 2 封" in content


def test_mailbox_sync_queued_mode_reported(client, manager, unified_mailbox, monkeypatch):
    monkeypatch.setattr(
        "apps.routing.views.dispatch", lambda func, *args, **kwargs: ("queued", None)
    )
    client.force_login(manager)
    response = client.post(
        reverse("routing:mailbox_overview"),
        {"mailbox_id": unified_mailbox.pk},
        follow=True,
    )
    assert response.status_code == 200
    assert "已加入队列" in response.content.decode()


def test_mailbox_sync_error_is_reported_not_500(client, manager, unified_mailbox, monkeypatch):
    def boom(func, *args, **kwargs):
        raise RuntimeError("IMAP 连接失败")

    monkeypatch.setattr("apps.routing.views.dispatch", boom)
    client.force_login(manager)

    response = client.post(
        reverse("routing:mailbox_overview"),
        {"mailbox_id": unified_mailbox.pk},
        follow=True,
    )
    assert response.status_code == 200
    assert "同步失败" in response.content.decode()


def test_mailbox_sync_unknown_id_is_reported(client, manager):
    client.force_login(manager)
    response = client.post(
        reverse("routing:mailbox_overview"), {"mailbox_id": "999999"}, follow=True
    )
    assert response.status_code == 200
    assert "未找到要同步的邮箱" in response.content.decode()
