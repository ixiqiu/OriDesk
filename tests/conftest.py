"""§12 验收测试的公共 fixture。

关键约定：所有测试都在离线环境运行——SMTP 出口被 autouse fixture 拦截，
`outbox` 收集"已发出"的邮件，测试通过它断言 From/Reply-To/主题等。
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.accounts.models import Group, Mailbox, User, UserGroup


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    """拦截真实发信（唯一网络出口），返回已发邮件列表。

    注意：必须 patch `apps.mailboxes.services.smtp_send`，
    autoresponder/mailboxes 都通过模块属性在调用时查找该函数。
    """
    sent: list[dict] = []

    def fake_send(mailbox, msg, **kwargs):
        sent.append({"mailbox": mailbox, "msg": msg})
        return None

    monkeypatch.setattr("apps.mailboxes.services.smtp_send", fake_send)
    return sent


@pytest.fixture
def sent_mail(outbox):
    return outbox


def make_mailbox(
    *,
    email: str,
    name: str | None = None,
    is_fallback: bool = False,
    is_active: bool = True,
    secret: str = "demo-auth-code",
) -> Mailbox:
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
    mailbox.set_secret(secret)
    mailbox.save()
    return mailbox


@pytest.fixture
def unified_mailbox(db) -> Mailbox:
    """统一进线邮箱（未绑定组）：需要走规则/粘性/兜底路由。"""
    return make_mailbox(email="support@example.com", name="统一进线邮箱")


@pytest.fixture
def authenticated_rule(unified_mailbox, tech_group):
    """让统一邮箱收到含"登录"的邮件时能路由到技术支持组（避免无可用组而抛错）。"""
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


@pytest.fixture
def fallback_mailbox(db) -> Mailbox:
    """全局兜底邮箱（全局唯一）。"""
    return make_mailbox(email="fallback@example.com", name="全局兜底邮箱", is_fallback=True)


@pytest.fixture
def tech_group(db) -> Group:
    return Group.objects.create(name="技术支持组")


@pytest.fixture
def finance_group(db) -> Group:
    return Group.objects.create(name="财务组")


@pytest.fixture
def admin_group(db) -> Group:
    return Group.objects.create(name="管理员组", is_admin_group=True)


@pytest.fixture
def tech_mailbox(db, tech_group) -> Mailbox:
    mailbox = make_mailbox(email="tech@example.com", name="技术支持组邮箱")
    tech_group.mailbox = mailbox
    tech_group.save(update_fields=["mailbox"])
    return mailbox


@pytest.fixture
def superadmin(db) -> User:
    """应用层超级管理员（同时具备 Django Admin 的 staff 权限，与 seed_demo 一致）。"""
    user = User.objects.create_user(
        username="superadmin",
        password="DemoPass!2345",
        is_superadmin=True,
        is_staff=True,
        is_superuser=True,
    )
    return user


@pytest.fixture
def tech_user(db, tech_group) -> User:
    user = User.objects.create_user(username="tech1", password="DemoPass!2345")
    UserGroup.objects.create(user=user, group=tech_group)
    return user


@pytest.fixture
def finance_user(db, finance_group) -> User:
    user = User.objects.create_user(username="finance1", password="DemoPass!2345")
    UserGroup.objects.create(user=user, group=finance_group)
    return user


@pytest.fixture
def admin_group_user(db, admin_group) -> User:
    """管理员组成员（非超级管理员），按 §2.7/§5.6 可跨组查看。"""
    user = User.objects.create_user(username="adminmember", password="DemoPass!2345")
    UserGroup.objects.create(user=user, group=admin_group, is_admin=True)
    return user


def make_ticket(*, mailbox, group, customer_email="customer@customer-domain.com", subject="无法登录后台", last_message_at=None, **kwargs):
    from apps.tickets.models import Ticket

    now = last_message_at or timezone.now()
    return Ticket.objects.create(
        mailbox=mailbox,
        group=group,
        subject=subject,
        normalized_subject=subject,
        customer_email=customer_email,
        last_message_at=now,
        **kwargs,
    )


def make_message(*, ticket, mailbox, direction="in", message_id="", **kwargs):
    from apps.tickets.models import Message

    return Message.objects.create(
        ticket=ticket,
        mailbox=mailbox,
        direction=direction,
        type=kwargs.pop("type", "message"),
        message_id=message_id,
        from_addr=kwargs.pop("from_addr", ticket.customer_email if direction == "in" else mailbox.email),
        to_addr=kwargs.pop("to_addr", mailbox.email if direction == "in" else ticket.customer_email),
        subject=kwargs.pop("subject", ticket.subject),
        body_text=kwargs.pop("body_text", "正文"),
        sent_at=kwargs.pop("sent_at", timezone.now()),
        **kwargs,
    )
