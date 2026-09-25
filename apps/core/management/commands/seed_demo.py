"""生成演示数据，便于本地试用与端到端验收（不属于生产流程）。

用法：
    python manage.py seed_demo            # 幂等创建演示数据
    python manage.py seed_demo --reset    # 先清空业务数据再创建

演示账号统一密码：DemoPass!2345
  superadmin  超级管理员（在管理员组，可跨组查看）
  tech1/tech2 技术支持组成员
  finance1    财务组成员
  ops1        运维组成员（运维组无对外邮箱，回信走全局兜底邮箱）
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

DEMO_PASSWORD = "DemoPass!2345"

MAILBOXES = [
    # name, email, is_fallback, 绑定组
    ("统一进线邮箱", "support@example.com", False, None),
    ("全局兜底邮箱", "fallback@example.com", True, None),
    ("技术支持组邮箱", "tech@example.com", False, "技术支持组"),
    ("财务组邮箱", "finance@example.com", False, "财务组"),
    ("管理员组邮箱", "admin@example.com", False, "管理员组"),
]

GROUPS = [
    ("管理员组", True, "admin@example.com"),
    ("技术支持组", False, "tech@example.com"),
    ("财务组", False, "finance@example.com"),
    ("运维组", False, None),  # 无对外邮箱：回信走全局兜底
]

USERS = [
    ("superadmin", "超级管理员", True, [("管理员组", True)]),
    ("tech1", "技术一号", False, [("技术支持组", False)]),
    ("tech2", "技术二号", False, [("技术支持组", True)]),
    ("finance1", "财务一号", False, [("财务组", False)]),
    ("ops1", "运维一号", False, [("运维组", False)]),
]


class Command(BaseCommand):
    help = "创建演示用邮箱、用户组、用户、规则与示例工单"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="清空业务数据后重建")
        parser.add_argument("--with-ticket", action="store_true", help="额外创建一封示例工单")

    @transaction.atomic
    def handle(self, *args, **options):
        from apps.accounts.models import Group, Mailbox, User, UserGroup
        from apps.audit.models import Setting
        from apps.routing.models import Rule

        if options["reset"]:
            from apps.tickets.models import Ticket

            Ticket.objects.all().delete()
            Rule.objects.all().delete()
            Mailbox.objects.all().update(is_fallback=False)
            Mailbox.objects.all().delete()
            Group.objects.all().delete()
            User.objects.filter(is_superuser=False).delete()
            self.stdout.write(self.style.WARNING("已清空演示业务数据。"))

        for name, email, is_fallback, _group in MAILBOXES:
            mailbox, created = Mailbox.objects.get_or_create(
                email=email,
                defaults={
                    "name": name,
                    "imap_host": "imap.feishu.cn",
                    "imap_port": 993,
                    "imap_ssl": True,
                    "smtp_host": "smtp.feishu.cn",
                    "smtp_port": 465,
                    "smtp_ssl": True,
                    "username": email,
                    "secret_encrypted": b"",
                    "is_fallback": is_fallback,
                },
            )
            if created:
                mailbox.set_secret("demo-authorization-code")
                mailbox.save()
                self.stdout.write(f"  邮箱 {email} 已创建（演示凭据）")

        for group_name, is_admin_group, mailbox_email in GROUPS:
            mailbox = Mailbox.objects.filter(email=mailbox_email).first() if mailbox_email else None
            Group.objects.update_or_create(
                name=group_name,
                defaults={"is_admin_group": is_admin_group, "mailbox": mailbox},
            )

        for username, full_name, is_superadmin, memberships in USERS:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "first_name": full_name,
                    "email": f"{username}@example.com",
                    "is_superadmin": is_superadmin,
                    "is_staff": is_superadmin,
                    "is_superuser": is_superadmin,
                },
            )
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()
                self.stdout.write(f"  用户 {username} 已创建")
            for group_name, is_admin in memberships:
                group = Group.objects.get(name=group_name)
                UserGroup.objects.get_or_create(
                    user=user, group=group, defaults={"is_admin": is_admin}
                )

        tech = Group.objects.get(name="技术支持组")
        finance = Group.objects.get(name="财务组")
        unified = Mailbox.objects.get(email="support@example.com")
        fallback = Mailbox.objects.get(email="fallback@example.com")

        Rule.objects.get_or_create(
            mailbox=unified,
            match_field="subject",
            match_op="contains",
            match_value="发票",
            defaults={
                "priority": 10,
                "action_type": "assign_group",
                "action_value": str(finance.pk),
            },
        )
        Rule.objects.get_or_create(
            mailbox=unified,
            match_field="subject",
            match_op="contains",
            match_value="无法登录",
            defaults={
                "priority": 20,
                "action_type": "assign_group",
                "action_value": str(tech.pk),
            },
        )

        Setting.set("fallback_group_id", tech.pk)
        Setting.set("fallback_mailbox_id", fallback.pk)

        if options["with_ticket"]:
            self._create_demo_ticket(unified, tech)

        self.stdout.write(self.style.SUCCESS("演示数据准备完成。"))
        self.stdout.write("登录账号：superadmin / tech1 / tech2 / finance1 / ops1")
        self.stdout.write(f"统一密码：{DEMO_PASSWORD}")

    def _create_demo_ticket(self, mailbox, group):
        from apps.tickets.models import Message
        from apps.tickets.services import create_ticket

        now = timezone.now()
        ticket = create_ticket(
            mailbox=mailbox,
            group=group,
            subject="[示例] 无法登录后台",
            customer_email="customer@customer-domain.com",
            now=now,
        )
        Message.objects.create(
            ticket=ticket,
            mailbox=mailbox,
            message_id="demo-message-1@customer-domain.com",
            direction="in",
            type="message",
            from_addr="customer@customer-domain.com",
            to_addr=mailbox.email,
            subject=ticket.subject,
            body_text="你好，我这边登录后台一直提示密码错误，麻烦帮忙看一下。",
            sent_at=now,
        )
        ticket.is_awaiting_reply = True
        ticket.save(update_fields=["is_awaiting_reply"])
        self.stdout.write(f"  示例工单 [T#{ticket.pk}] 已创建")
