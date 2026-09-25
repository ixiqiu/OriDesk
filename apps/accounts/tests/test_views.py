"""账号与邮箱管理界面的视图/表单测试（开发文档 §2.1 / §2.7 / §10.3）。

覆盖：
- 未登录访问管理页面被重定向到登录页；普通用户访问管理页面 403。
- 超级管理员可创建用户/组/邮箱。
- 密码留空不改、填写可通过 check_password 校验。
- 组成员与组内管理员保存正确且无重复。
- 邮箱凭据密文存储、响应中无明文。
- 最后一个超级管理员不能被降级/停用。
- 无邮箱组的列表提示、mailbox_verify 异常不 500。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Group, Mailbox, User, UserGroup
from apps.tickets.models import Ticket

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def superadmin():
    return User.objects.create_user(
        username="root",
        password="RootPass-12345",
        email="root@example.com",
        is_superadmin=True,
        is_active=True,
    )


@pytest.fixture
def normal_user():
    return User.objects.create_user(
        username="staff",
        password="StaffPass-12345",
        is_active=True,
    )


@pytest.fixture
def admin_client(client, superadmin):
    client.force_login(superadmin)
    return client


@pytest.fixture
def user_client(client, normal_user):
    client.force_login(normal_user)
    return client


@pytest.fixture
def group():
    return Group.objects.create(name="技术支持")


@pytest.fixture
def mailbox():
    box = Mailbox.objects.create(
        name="统一进线",
        email="support@example.com",
        imap_host="imap.example.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_ssl=True,
        username="support@example.com",
    )
    box.set_secret("fallback-auth-code")
    box.save()
    return box


def _ticket(group, user, mailbox) -> Ticket:
    return Ticket.objects.create(
        mailbox=mailbox,
        group=group,
        subject="无法登录",
        normalized_subject="无法登录",
        customer_email="customer@example.com",
        last_message_at=timezone.now() - timedelta(minutes=5),
    )


# ------------------------------------------------------------------ 登录保护
LOGIN_PROTECTED_NAMES = [
    "accounts:profile",
    "accounts:user_list",
    "accounts:user_create",
    "accounts:group_list",
    "accounts:group_create",
    "accounts:mailbox_list",
    "accounts:mailbox_create",
    "accounts:password_change",
]


@pytest.mark.parametrize("url_name", LOGIN_PROTECTED_NAMES)
def test_anonymous_redirected_to_login(client, url_name):
    response = client.get(reverse(url_name))
    assert response.status_code == 302
    assert reverse("accounts:login") in response["Location"]


def test_anonymous_redirected_for_edit_pages(client, group, mailbox, superadmin):
    for url_name, pk in (
        ("accounts:user_edit", superadmin.pk),
        ("accounts:group_edit", group.pk),
        ("accounts:mailbox_edit", mailbox.pk),
    ):
        response = client.get(reverse(url_name, args=[pk]))
        assert response.status_code == 302
        assert reverse("accounts:login") in response["Location"]


MANAGEMENT_NAMES = [
    "accounts:user_list",
    "accounts:user_create",
    "accounts:group_list",
    "accounts:group_create",
    "accounts:mailbox_list",
    "accounts:mailbox_create",
]


@pytest.mark.parametrize("url_name", MANAGEMENT_NAMES)
def test_normal_user_forbidden_on_management(user_client, url_name):
    assert user_client.get(reverse(url_name)).status_code == 403


def test_normal_user_forbidden_on_edit_pages(user_client, group, mailbox, normal_user):
    for url_name, pk in (
        ("accounts:user_edit", normal_user.pk),
        ("accounts:group_edit", group.pk),
        ("accounts:mailbox_edit", mailbox.pk),
    ):
        assert user_client.get(reverse(url_name, args=[pk])).status_code == 403


def test_normal_user_forbidden_on_mailbox_verify(user_client, mailbox):
    response = user_client.post(reverse("accounts:mailbox_verify", args=[mailbox.pk]))
    assert response.status_code == 403


def test_login_page_renders_standalone(client):
    response = client.get(reverse("accounts:login"))
    body = response.content.decode()
    assert response.status_code == 200
    assert "auth-page" in body
    assert "auth-card" in body


# ------------------------------------------------------------------ 个人资料
def test_profile_shows_groups_and_visible_ticket_count(user_client, normal_user, group, mailbox):
    UserGroup.objects.create(user=normal_user, group=group, is_admin=True)
    _ticket(group, normal_user, mailbox)

    response = user_client.get(reverse("accounts:profile"))
    body = response.content.decode()

    assert response.status_code == 200
    assert normal_user.username in body
    assert group.name in body
    assert "组内管理员" in body
    assert "可见工单数" in body
    assert "1" in body


def test_profile_ticket_count_uses_visibility(user_client, normal_user, group, mailbox, superadmin):
    _ticket(group, normal_user, mailbox)
    other_group = Group.objects.create(name="其他组")
    _ticket(other_group, superadmin, mailbox)

    response = user_client.get(reverse("accounts:profile"))
    assert response.context["visible_ticket_count"] == 0


# ------------------------------------------------------------------ 用户管理
def test_superadmin_user_list_with_search_and_group_filter(admin_client, group, normal_user):
    UserGroup.objects.create(user=normal_user, group=group)
    User.objects.create_user(username="other", password="OtherPass-12345")

    response = admin_client.get(reverse("accounts:user_list"))
    assert response.status_code == 200
    assert response.context["page_obj"].paginator.per_page == 25

    filtered = admin_client.get(reverse("accounts:user_list"), {"group": group.pk})
    usernames = [u.username for u in filtered.context["users"]]
    assert usernames == [normal_user.username]

    searched = admin_client.get(reverse("accounts:user_list"), {"q": "oth"})
    assert [u.username for u in searched.context["users"]] == ["other"]


def test_superadmin_creates_user_with_group_admin(admin_client, group):
    response = admin_client.post(
        reverse("accounts:user_create"),
        {
            "username": "newbie",
            "first_name": "新",
            "last_name": "同事",
            "email": "newbie@example.com",
            "is_active": "on",
            "password": "NewPass-12345",
            "groups": [str(group.pk)],
            f"membership_admin_{group.pk}": "on",
        },
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:user_list")

    created = User.objects.get(username="newbie")
    assert created.check_password("NewPass-12345")
    assert created.is_superadmin is False
    membership = UserGroup.objects.get(user=created, group=group)
    assert membership.is_admin is True
    assert UserGroup.objects.filter(user=created).count() == 1


def test_user_form_get_renders_admin_checkboxes(admin_client, group, normal_user):
    UserGroup.objects.create(user=normal_user, group=group, is_admin=True)

    create_page = admin_client.get(reverse("accounts:user_create"))
    assert create_page.status_code == 200
    assert f'name="membership_admin_{group.pk}"' in create_page.content.decode()

    edit_page = admin_client.get(reverse("accounts:user_edit", args=[normal_user.pk]))
    body = edit_page.content.decode()
    assert edit_page.status_code == 200
    assert f'name="membership_admin_{group.pk}"' in body
    # 已是组内管理员 → 复选框默认勾选
    assert f'name="membership_admin_{group.pk}" id="id_membership_admin_{group.pk}" checked' in body


def test_create_user_requires_password(admin_client):
    response = admin_client.post(
        reverse("accounts:user_create"),
        {"username": "nopass", "is_active": "on", "password": ""},
    )
    assert response.status_code == 200
    assert not User.objects.filter(username="nopass").exists()
    assert "必须设置密码" in response.content.decode()


def test_edit_user_password_blank_keeps_old(admin_client):
    target = User.objects.create_user(username="editme", password="OldPass-12345")

    response = admin_client.post(
        reverse("accounts:user_edit", args=[target.pk]),
        {
            "username": "editme",
            "first_name": "改",
            "last_name": "",
            "email": "editme@example.com",
            "is_active": "on",
            "password": "",
        },
    )

    assert response.status_code == 302
    target.refresh_from_db()
    assert target.check_password("OldPass-12345")
    assert target.first_name == "改"


def test_edit_user_password_filled_is_rehashed(admin_client):
    target = User.objects.create_user(username="editme2", password="OldPass-12345")

    response = admin_client.post(
        reverse("accounts:user_edit", args=[target.pk]),
        {
            "username": "editme2",
            "first_name": "",
            "last_name": "",
            "email": "",
            "is_active": "on",
            "password": "BrandNewPass-98765",
        },
    )

    assert response.status_code == 302
    target.refresh_from_db()
    assert target.check_password("BrandNewPass-98765")
    assert not target.check_password("OldPass-12345")


def test_edit_user_group_membership_synced_without_duplicates(admin_client, group):
    target = User.objects.create_user(username="member", password="MemberPass-12345")
    other_group = Group.objects.create(name="售后")
    url = reverse("accounts:user_edit", args=[target.pk])
    payload = {
        "username": "member",
        "first_name": "",
        "last_name": "",
        "email": "",
        "is_active": "on",
        "password": "",
        "groups": [str(group.pk), str(other_group.pk)],
        f"membership_admin_{other_group.pk}": "on",
    }

    admin_client.post(url, payload)
    admin_client.post(url, payload)

    assert UserGroup.objects.filter(user=target).count() == 2
    assert UserGroup.objects.get(user=target, group=other_group).is_admin is True
    assert UserGroup.objects.get(user=target, group=group).is_admin is False

    # 取消勾选 other_group 后成员关系被清理，且不影响其他组的成员
    admin_client.post(url, {**payload, "groups": [str(group.pk)]})
    assert UserGroup.objects.filter(user=target).count() == 1
    assert UserGroup.objects.filter(user=target, group=group).exists()


def test_last_superadmin_cannot_be_demoted(admin_client, superadmin):
    response = admin_client.post(
        reverse("accounts:user_edit", args=[superadmin.pk]),
        {
            "username": superadmin.username,
            "first_name": "",
            "last_name": "",
            "email": superadmin.email,
            "is_active": "on",
            "password": "",
        },
    )

    assert response.status_code == 200
    assert "最后一个超级管理员" in response.content.decode()
    superadmin.refresh_from_db()
    assert superadmin.is_superadmin is True
    assert superadmin.is_active is True


def test_last_superadmin_cannot_be_deactivated(admin_client, superadmin):
    response = admin_client.post(
        reverse("accounts:user_edit", args=[superadmin.pk]),
        {
            "username": superadmin.username,
            "first_name": "",
            "last_name": "",
            "email": superadmin.email,
            "is_superadmin": "on",
            "password": "",
        },
    )

    assert response.status_code == 200
    assert "最后一个超级管理员" in response.content.decode()
    superadmin.refresh_from_db()
    assert superadmin.is_active is True


def test_superadmin_can_be_demoted_when_another_exists(admin_client, superadmin):
    User.objects.create_user(username="root2", password="RootPass-12345", is_superadmin=True, is_active=True)
    response = admin_client.post(
        reverse("accounts:user_edit", args=[superadmin.pk]),
        {
            "username": superadmin.username,
            "first_name": "",
            "last_name": "",
            "email": superadmin.email,
            "is_active": "on",
            "password": "",
        },
    )
    assert response.status_code == 302
    superadmin.refresh_from_db()
    assert superadmin.is_superadmin is False


# ------------------------------------------------------------------ 用户组管理
def test_superadmin_creates_group(admin_client, mailbox):
    response = admin_client.post(
        reverse("accounts:group_create"),
        {"name": "售后组", "mailbox": str(mailbox.pk), "is_admin_group": "on"},
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:group_list")

    created = Group.objects.get(name="售后组")
    assert created.mailbox_id == mailbox.pk
    assert created.is_admin_group is True


def test_group_mailbox_choices_exclude_taken_mailboxes(admin_client, mailbox):
    Group.objects.create(name="占用组", mailbox=mailbox)

    response = admin_client.get(reverse("accounts:group_create"))
    assert response.status_code == 200
    choices = list(response.context["form"].fields["mailbox"].queryset)
    assert mailbox not in choices

    bound_group = Group.objects.get(name="占用组")
    edit_response = admin_client.get(reverse("accounts:group_edit", args=[bound_group.pk]))
    edit_choices = list(edit_response.context["form"].fields["mailbox"].queryset)
    assert mailbox in edit_choices


def test_group_members_and_admins_saved_without_duplicates(admin_client):
    first = User.objects.create_user(username="u1", password="UserPass-12345")
    second = User.objects.create_user(username="u2", password="UserPass-12345")
    url = reverse("accounts:group_create")
    payload = {
        "name": "客服组",
        "is_admin_group": "on",
        "members": [str(first.pk), str(second.pk)],
        f"member_admin_{first.pk}": "on",
    }

    admin_client.post(url, payload)

    created = Group.objects.get(name="客服组")
    assert UserGroup.objects.filter(group=created).count() == 2
    assert UserGroup.objects.get(group=created, user=first).is_admin is True
    assert UserGroup.objects.get(group=created, user=second).is_admin is False
    assert UserGroup.objects.filter(group=created, user=first).count() == 1

    # 再次提交相同数据不会产生重复成员关系
    admin_client.post(reverse("accounts:group_edit", args=[created.pk]), payload)
    assert UserGroup.objects.filter(group=created).count() == 2


def test_group_list_hints_missing_mailbox(admin_client):
    Group.objects.create(name="无邮箱组")

    response = admin_client.get(reverse("accounts:group_list"))
    body = response.content.decode()

    assert response.status_code == 200
    assert "无邮箱，回信走全局兜底邮箱" in body


def test_group_list_warns_when_no_admin_group(admin_client):
    Group.objects.create(name="普通组")

    response = admin_client.get(reverse("accounts:group_list"))
    assert response.status_code == 200
    assert "alert-warning" in response.content.decode()
    assert response.context["has_admin_group"] is False


# ------------------------------------------------------------------ 邮箱配置
def test_superadmin_creates_mailbox_with_encrypted_secret(admin_client):
    response = admin_client.post(
        reverse("accounts:mailbox_create"),
        {
            "name": "统一进线",
            "email": "box@example.com",
            "imap_host": "imap.example.com",
            "imap_port": "993",
            "imap_ssl": "on",
            "smtp_host": "smtp.example.com",
            "smtp_port": "465",
            "smtp_ssl": "on",
            "username": "box@example.com",
            "is_fallback": "on",
            "is_active": "on",
            "secret": "PlainText-AuthCode",
        },
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:mailbox_list")

    created = Mailbox.objects.get(email="box@example.com")
    assert created.get_secret() == "PlainText-AuthCode"
    assert b"PlainText-AuthCode" not in bytes(created.secret_encrypted)
    assert created.is_fallback is True


def test_mailbox_create_requires_secret(admin_client):
    response = admin_client.post(
        reverse("accounts:mailbox_create"),
        {
            "name": "无凭据",
            "email": "nosecret@example.com",
            "imap_host": "imap.example.com",
            "imap_port": "993",
            "smtp_host": "smtp.example.com",
            "smtp_port": "465",
            "username": "nosecret@example.com",
            "is_active": "on",
            "secret": "",
        },
    )
    assert response.status_code == 200
    assert not Mailbox.objects.filter(email="nosecret@example.com").exists()
    assert "必须填写授权码" in response.content.decode()


def test_mailbox_secret_never_rendered_in_html(admin_client, mailbox):
    for url in (
        reverse("accounts:mailbox_list"),
        reverse("accounts:mailbox_edit", args=[mailbox.pk]),
    ):
        body = admin_client.get(url).content.decode()
        assert "fallback-auth-code" not in body
        assert set(mailbox.secret_masked) == {"x", "*"}


def test_edit_mailbox_blank_secret_keeps_credential(admin_client, mailbox):
    response = admin_client.post(
        reverse("accounts:mailbox_edit", args=[mailbox.pk]),
        {
            "name": mailbox.name,
            "email": mailbox.email,
            "imap_host": mailbox.imap_host,
            "imap_port": str(mailbox.imap_port),
            "imap_ssl": "on",
            "smtp_host": mailbox.smtp_host,
            "smtp_port": str(mailbox.smtp_port),
            "smtp_ssl": "on",
            "username": mailbox.username,
            "is_active": "on",
            "secret": "",
        },
    )

    assert response.status_code == 302
    mailbox.refresh_from_db()
    assert mailbox.get_secret() == "fallback-auth-code"


def test_second_fallback_mailbox_rejected(admin_client, mailbox):
    mailbox.is_fallback = True
    mailbox.save()
    other = Mailbox.objects.create(
        name="另一个",
        email="other@example.com",
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=465,
        username="other@example.com",
    )
    other.set_secret("another-secret")
    other.save()

    response = admin_client.post(
        reverse("accounts:mailbox_edit", args=[other.pk]),
        {
            "name": other.name,
            "email": other.email,
            "imap_host": other.imap_host,
            "imap_port": str(other.imap_port),
            "imap_ssl": "on",
            "smtp_host": other.smtp_host,
            "smtp_port": str(other.smtp_port),
            "smtp_ssl": "on",
            "username": other.username,
            "is_active": "on",
            "is_fallback": "on",
            "secret": "",
        },
    )

    assert response.status_code == 200
    assert "全局只能有一个" in response.content.decode()
    other.refresh_from_db()
    assert other.is_fallback is False


# ------------------------------------------------------------------ 连通性检查
class _FakeIMAPClient:
    """假 IMAP 客户端（配合 apps/mailboxes/imap_client.connect_imap 使用）。

    邮箱默认 imap_ssl=True，因此 connect_imap 不会走 STARTTLS 分支；
    这里仍实现 has_capability 以便断言加密策略相关的调用。
    """

    def __init__(self, host, port=None, ssl=None, timeout=None):
        self.host = host
        self.port = port
        self.ssl = ssl
        self.logged_out = False
        self.starttls_called = False

    def has_capability(self, name):
        return str(name).upper() == "STARTTLS"

    def capabilities(self):
        return (b"IMAP4REV1", b"STARTTLS")

    def starttls(self, ssl_context=None):
        self.starttls_called = True

    def login(self, username, secret):
        self.username = username
        self.secret = secret

    def select_folder(self, name):
        assert name == "INBOX"
        return {b"EXISTS": 7}

    def logout(self):
        self.logged_out = True


class _FakeSMTP:
    """假 SMTP：只有 login/quit，若视图误发信会直接 AttributeError。"""

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port

    def login(self, username, secret):
        self.username = username

    def quit(self):
        self.quit_called = True


def test_mailbox_verify_success(admin_client, mailbox, monkeypatch):
    monkeypatch.setattr("imapclient.IMAPClient", _FakeIMAPClient)
    monkeypatch.setattr("smtplib.SMTP_SSL", _FakeSMTP)

    response = admin_client.post(reverse("accounts:mailbox_verify", args=[mailbox.pk]), follow=True)
    body = response.content.decode()

    assert response.status_code == 200
    assert "IMAP 登录成功，INBOX 共 7 封" in body


def test_mailbox_verify_reports_imap_error_instead_of_500(admin_client, mailbox, monkeypatch):
    class _BoomIMAP:
        def __init__(self, *args, **kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr("imapclient.IMAPClient", _BoomIMAP)

    response = admin_client.post(reverse("accounts:mailbox_verify", args=[mailbox.pk]), follow=True)
    body = response.content.decode()

    assert response.status_code == 200
    assert "连通性检查失败" in body


def test_mailbox_verify_does_not_leak_secret_on_error(admin_client, mailbox, monkeypatch):
    class _BoomIMAP:
        def __init__(self, *args, **kwargs):
            raise OSError("auth failed for fallback-auth-code")

    monkeypatch.setattr("imapclient.IMAPClient", _BoomIMAP)

    response = admin_client.post(reverse("accounts:mailbox_verify", args=[mailbox.pk]), follow=True)
    assert "fallback-auth-code" not in response.content.decode()


def test_mailbox_verify_requires_post(admin_client, mailbox):
    assert admin_client.get(reverse("accounts:mailbox_verify", args=[mailbox.pk])).status_code == 405
