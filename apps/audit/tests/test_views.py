"""audit 管理界面视图测试（开发文档 §2.5 / §4.5 / §12.5）。

覆盖：权限边界、筛选、分页（50 条/页）、CSV 导出（BOM + 表头 + 记录）、详情页。
fixture 在本文件内自建，避免依赖其它测试包的 conftest。
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Group, Mailbox, User, UserGroup
from apps.audit.models import AuditLog
from apps.tickets.models import Ticket

pytestmark = pytest.mark.django_db

PASSWORD = "DemoPass!2345"


@pytest.fixture(autouse=True)
def _clear_setting_cache():
    cache.clear()
    yield
    cache.clear()


# ------------------------------------------------------------------ 工具
def make_mailbox(email="support@example.com"):
    mailbox = Mailbox(
        name="统一进线邮箱",
        email=email,
        imap_host="imap.example.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_ssl=True,
        username=email,
        secret_encrypted=b"",
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
def mailbox(db):
    return make_mailbox()


@pytest.fixture
def ticket(db, mailbox, tech_group):
    return Ticket.objects.create(
        mailbox=mailbox,
        group=tech_group,
        subject="无法登录后台",
        normalized_subject="无法登录后台",
        customer_email="customer@customer-domain.com",
        last_message_at=timezone.now(),
    )


# ------------------------------------------------------------------ 权限边界
def test_anonymous_is_redirected_to_login(client):
    response = client.get(reverse("audit:log_list"))
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.parametrize("url_name,args", [("audit:log_list", []), ("audit:log_detail", [1])])
def test_normal_user_forbidden(client, staff, url_name, args):
    client.force_login(staff)
    assert client.get(reverse(url_name, args=args)).status_code == 403


def test_manager_can_open_list_and_detail(client, manager, ticket):
    log = AuditLog.objects.create(
        user=manager, action="reply", ticket=ticket, group=ticket.group, detail={"subject": "x"}
    )
    client.force_login(manager)
    assert client.get(reverse("audit:log_list")).status_code == 200
    assert client.get(reverse("audit:log_detail", args=[log.pk])).status_code == 200


# ------------------------------------------------------------------ 筛选
def test_list_filters_by_action_and_keyword(client, manager, ticket):
    AuditLog.objects.create(user=manager, action="reply", ticket=ticket, detail={"subject": "发票"})
    AuditLog.objects.create(
        user=manager,
        action="config_change",
        identity_email="ops@example.com",
        detail={"key": "sticky_window_days", "value": "14"},
    )
    client.force_login(manager)

    response = client.get(reverse("audit:log_list"), {"action": "config_change"})
    assert response.status_code == 200
    logs = list(response.context["page_obj"].object_list)
    assert len(logs) == 1
    assert logs[0].action == "config_change"

    # 关键字命中 detail
    response = client.get(reverse("audit:log_list"), {"q": "sticky_window_days"})
    logs = list(response.context["page_obj"].object_list)
    assert len(logs) == 1
    assert logs[0].detail["key"] == "sticky_window_days"

    # 关键字命中 identity_email
    response = client.get(reverse("audit:log_list"), {"q": "ops@example.com"})
    assert len(list(response.context["page_obj"].object_list)) == 1

    # 按工单号筛选
    response = client.get(reverse("audit:log_list"), {"ticket_no": f"[T#{ticket.pk}]"})
    logs = list(response.context["page_obj"].object_list)
    assert len(logs) == 1
    assert logs[0].ticket_id == ticket.pk

    # 无匹配
    response = client.get(reverse("audit:log_list"), {"action": "login"})
    assert len(list(response.context["page_obj"].object_list)) == 0


def test_list_filters_by_user_and_date_range(client, manager, staff):
    AuditLog.objects.create(user=manager, action="reply")
    AuditLog.objects.create(user=staff, action="reply")
    client.force_login(manager)

    response = client.get(reverse("audit:log_list"), {"user": staff.pk})
    logs = list(response.context["page_obj"].object_list)
    assert len(logs) == 1
    assert logs[0].user_id == staff.pk

    today = timezone.localdate().isoformat()
    response = client.get(reverse("audit:log_list"), {"date_from": today, "date_to": today})
    assert len(list(response.context["page_obj"].object_list)) == 2


# ------------------------------------------------------------------ 分页
def test_list_paginates_50_per_page(client, manager):
    AuditLog.objects.bulk_create(
        [AuditLog(user=manager, action="reply", detail={"index": i}) for i in range(55)]
    )
    client.force_login(manager)

    response = client.get(reverse("audit:log_list"))
    page_obj = response.context["page_obj"]
    assert page_obj.paginator.per_page == 50
    assert page_obj.paginator.num_pages == 2
    assert len(page_obj.object_list) == 50

    response = client.get(reverse("audit:log_list"), {"page": 2})
    assert len(response.context["page_obj"].object_list) == 5


# ------------------------------------------------------------------ CSV 导出
def test_csv_export_contains_bom_header_and_records(client, manager, ticket):
    AuditLog.objects.create(
        user=manager,
        action="reply",
        ticket=ticket,
        group=ticket.group,
        identity_email="tech@example.com",
        detail={"subject": "无法登录后台"},
    )
    client.force_login(manager)

    response = client.get(reverse("audit:log_list"), {"export": "csv"})
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert "attachment" in response["Content-Disposition"]
    assert ".csv" in response["Content-Disposition"]

    raw = response.content
    assert raw.startswith("\ufeff".encode("utf-8"))
    text = raw.decode("utf-8-sig")
    assert "时间,动作,操作人,工单号,组,对外身份,摘要" in text
    assert "回复" in text
    assert f"[T#{ticket.pk}]" in text
    assert "tech@example.com" in text


def test_csv_export_honours_current_filters(client, manager):
    AuditLog.objects.create(user=manager, action="reply")
    AuditLog.objects.create(user=manager, action="login")
    client.force_login(manager)

    response = client.get(reverse("audit:log_list"), {"action": "login", "export": "csv"})
    text = response.content.decode("utf-8-sig")
    assert "登录" in text
    assert "回复" not in text


# ------------------------------------------------------------------ 详情
def test_log_detail_shows_json_and_ticket_link(client, manager, ticket):
    log = AuditLog.objects.create(
        user=manager,
        action="forward",
        ticket=ticket,
        group=ticket.group,
        identity_email="tech@example.com",
        detail={"from_group": "技术支持组", "to_group": "财务组"},
    )
    client.force_login(manager)
    response = client.get(reverse("audit:log_detail", args=[log.pk]))
    assert response.status_code == 200
    assert response.context["ticket_visible"] is True
    content = response.content.decode()
    assert "from_group" in content
    assert "技术支持组" in content
    assert reverse("tickets:detail", args=[ticket.pk]) in content
