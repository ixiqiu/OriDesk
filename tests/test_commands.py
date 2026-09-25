"""管理命令验收用例（init_settings / seed_demo / runapscheduler --once）。"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from apps.accounts.models import Group, Mailbox, User, UserGroup
from apps.audit.models import Setting
from apps.autoresponder.services import DEFAULT_TEMPLATE
from apps.routing.models import Rule, Template


def test_init_settings_creates_defaults(db):
    call_command("init_settings", stdout=StringIO())

    for key in Setting.DEFAULTS:
        assert Setting.objects.filter(key=key).exists()
    assert Template.objects.filter(scope="global").exists()
    assert Template.objects.get(scope="global").body == DEFAULT_TEMPLATE


def test_init_settings_is_idempotent(db):
    call_command("init_settings", stdout=StringIO())
    Setting.set("sticky_window_days", 3)
    call_command("init_settings", stdout=StringIO())
    assert Setting.get("sticky_window_days") == "3"


def test_init_settings_force_resets(db):
    call_command("init_settings", stdout=StringIO())
    Setting.set("sticky_window_days", 3)
    call_command("init_settings", "--force", stdout=StringIO())
    assert Setting.get("sticky_window_days") == "7"


def test_seed_demo_creates_and_is_idempotent(db):
    out = StringIO()
    call_command("seed_demo", stdout=out)
    call_command("seed_demo", stdout=out)

    assert Mailbox.objects.filter(email="support@example.com").exists()
    assert Mailbox.objects.filter(is_fallback=True).count() == 1
    assert Group.objects.filter(is_admin_group=True).count() == 1
    assert Group.objects.get(name="运维组").mailbox is None
    assert User.objects.filter(username="superadmin", is_superadmin=True).exists()
    assert UserGroup.objects.filter(user__username="tech1").count() == 1
    assert Rule.objects.count() == 2
    assert Setting.get_int("fallback_group_id") > 0

    # 演示邮箱凭据是加密存储的，能解出原文
    mailbox = Mailbox.objects.get(email="support@example.com")
    assert mailbox.get_secret() == "demo-authorization-code"


def test_seed_demo_with_ticket(db):
    call_command("seed_demo", "--with-ticket", stdout=StringIO())
    from apps.tickets.models import Ticket

    ticket = Ticket.objects.get()
    assert ticket.is_awaiting_reply is True
    assert ticket.group.name == "技术支持组"


def test_runapscheduler_once(db, monkeypatch):
    calls = {}

    def fake_sync_all():
        calls["called"] = True
        return [{"mailbox": "support@example.com", "processed": 2}]

    monkeypatch.setattr("apps.mailboxes.sync.sync_all_mailboxes", fake_sync_all)
    out = StringIO()
    call_command("runapscheduler", "--once", stdout=out)

    assert calls.get("called") is True
    assert "processed" in out.getvalue()


@pytest.mark.django_db
def test_runapscheduler_interval_argument(db, monkeypatch):
    """--once 之外的参数解析不应报错（不真正启动调度器）。"""
    from apps.core.management.commands import runapscheduler as module

    monkeypatch.setattr("apps.mailboxes.sync.sync_all_mailboxes", lambda limit=None: [])
    command = module.Command()
    parser = command.create_parser("manage.py", "runapscheduler")
    options = parser.parse_args(["--interval", "5"])
    assert options.interval == 5
