"""三处钩子的接线测试（契约 §4.4）。

测的是「事件有没有被正确触发、带着什么参数」——受众/聚合/文案由
`test_notifications_services.py` 覆盖，HTTP 由 `test_notifications_ntfy.py` 覆盖。

**为什么打桩 `enqueue_notify` 而不是打桩 `ntfy.publish`**：三处钩子都在函数内部
`from apps.notifications.tasks import enqueue_notify`，运行时解析模块属性，
所以打桩它正好切在「钩子接线」这一层，断言最干净，也不受 RQ/Redis 是否存在影响。
"""

from __future__ import annotations

import pytest
from django.db import transaction

from apps.mailboxes.pipeline import process_inbound
from apps.notifications.audience import (
    EVENT_NOTE_MENTIONED,
    EVENT_TICKET_CREATED,
    EVENT_TICKET_INBOUND,
    EVENT_TICKET_REASSIGNED,
)
from apps.notifications.tasks import enqueue_notify, notify_event_task
from apps.routing.models import Rule
from apps.routing.services import reassign_ticket
from apps.tickets.services import add_note
from tests.conftest import make_ticket
from tests.helpers import build_raw

pytestmark = pytest.mark.django_db

CUSTOMER = "customer@customer-domain.com"


@pytest.fixture
def hook_calls(monkeypatch):
    """记录钩子对 enqueue_notify 的调用。"""
    calls: list[dict] = []

    def fake_enqueue(event, ticket, **kwargs):
        calls.append({"event": event, "ticket": ticket, **kwargs})
        return "inline"

    monkeypatch.setattr("apps.notifications.tasks.enqueue_notify", fake_enqueue)
    return calls


@pytest.fixture
def login_rule(unified_mailbox, tech_group):
    return Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="登录",
        action_type="assign_group",
        action_value=str(tech_group.pk),
    )


class TestPipelineHook:
    def test_new_ticket_emits_created(self, login_rule, unified_mailbox, hook_calls):
        process_inbound(
            unified_mailbox, 1, build_raw(sender=CUSTOMER, subject="无法登录后台")
        )
        assert len(hook_calls) == 1
        assert hook_calls[0]["event"] == EVENT_TICKET_CREATED
        assert hook_calls[0]["ticket"].pk is not None
        assert hook_calls[0]["subject"] == "无法登录后台"

    def test_followup_message_emits_inbound(
        self, login_rule, unified_mailbox, hook_calls
    ):
        first = process_inbound(
            unified_mailbox, 1, build_raw(sender=CUSTOMER, subject="无法登录后台")
        )
        # 归并靠主题里的工单号（同一发件人）—— 见 tests/test_merge.py 的口径。
        second = process_inbound(
            unified_mailbox,
            2,
            build_raw(sender=CUSTOMER, subject=f"[T#{first.ticket.pk}] 无法登录后台"),
        )
        assert second.created is False
        assert second.ticket.pk == first.ticket.pk
        assert hook_calls[-1]["event"] == EVENT_TICKET_INBOUND

    def test_loop_mail_emits_nothing(self, unified_mailbox, hook_calls):
        """防循环丢弃的邮件不该产生通知（契约 §4.4 / `02` §6）。"""
        process_inbound(
            unified_mailbox,
            3,
            build_raw(
                sender=CUSTOMER,
                subject="自动回复",
                headers={"Auto-Submitted": "auto-replied"},
            ),
        )
        assert hook_calls == []

    def test_notification_failure_does_not_break_intake(
        self, login_rule, unified_mailbox, monkeypatch
    ):
        """**最关键的一条**：通知炸了也必须把信收下来。

        与 `apply_rule_actions` / 自动回复同样的防御风格（`02-OriDesk后端侦察.md` §2.1）。
        """

        def boom(*args, **kwargs):
            raise RuntimeError("通知炸了")

        monkeypatch.setattr("apps.notifications.tasks.enqueue_notify", boom)
        result = process_inbound(
            unified_mailbox, 1, build_raw(sender=CUSTOMER, subject="无法登录后台")
        )
        assert result.status == "processed"
        assert result.created is True
        assert result.ticket.messages.filter(direction="in").count() == 1


class TestReassignHook:
    def test_emits_reassigned_with_actor(
        self, tech_group, finance_group, tech_user, unified_mailbox, hook_calls
    ):
        ticket = make_ticket(mailbox=unified_mailbox, group=tech_group)
        reassign_ticket(ticket, finance_group, tech_user, reason="归财务")
        assert len(hook_calls) == 1
        assert hook_calls[0]["event"] == EVENT_TICKET_REASSIGNED
        assert hook_calls[0]["actor"] == tech_user

    def test_ticket_group_is_already_new_one_when_notified(
        self, tech_group, finance_group, tech_user, unified_mailbox, hook_calls
    ):
        """通知发出时 `ticket.group` 必须已指向新组 —— 否则会推给已失去可见性的原组。"""
        ticket = make_ticket(mailbox=unified_mailbox, group=tech_group)
        reassign_ticket(ticket, finance_group, tech_user)
        assert hook_calls[0]["ticket"].group_id == finance_group.pk

    def test_failure_does_not_break_reassign(
        self, tech_group, finance_group, tech_user, unified_mailbox, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("通知炸了")

        monkeypatch.setattr("apps.notifications.tasks.enqueue_notify", boom)
        ticket = make_ticket(mailbox=unified_mailbox, group=tech_group)
        result = reassign_ticket(ticket, finance_group, tech_user)
        result.refresh_from_db()
        assert result.group_id == finance_group.pk  # 改派本身成功了


class TestNoteHook:
    def test_emits_mention_event_with_actor(
        self, tech_group, tech_user, tech_mailbox, hook_calls
    ):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        add_note(ticket, tech_user, "只是普通备注")
        assert len(hook_calls) == 1
        assert hook_calls[0]["event"] == EVENT_NOTE_MENTIONED
        assert hook_calls[0]["actor"] == tech_user
        assert hook_calls[0]["mentioned"] == []

    def test_resolves_mentions(self, tech_group, tech_user, admin_group_user, tech_mailbox, hook_calls):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        add_note(ticket, tech_user, "请 @adminmember 看一下")
        assert [u.pk for u in hook_calls[0]["mentioned"]] == [admin_group_user.pk]

    def test_email_in_note_body_is_not_a_mention(
        self, tech_group, tech_user, tech_mailbox, hook_calls
    ):
        """端到端回归：备注里写客户邮箱，不能变成一条提及。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        add_note(ticket, tech_user, "客户邮箱是 customer@customer-domain.com")
        assert hook_calls[0]["mentioned"] == []

    def test_failure_does_not_break_note(
        self, tech_group, tech_user, tech_mailbox, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("通知炸了")

        monkeypatch.setattr("apps.notifications.tasks.enqueue_notify", boom)
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        message = add_note(ticket, tech_user, "备注内容")
        assert message.pk is not None
        assert ticket.messages.filter(type="note").count() == 1


class TestEnqueueNotifySemantics:
    """真实 `enqueue_notify` 的事务语义（不打桩）。"""

    def test_defers_when_inside_transaction(
        self, tech_group, tech_mailbox, django_capture_on_commit_callbacks, monkeypatch
    ):
        """`add_note` 带 @transaction.atomic：必须等提交后才入队。

        否则 RQ worker 可能立刻执行并读到**未提交**的数据库状态
        （其他连接看不到这些行），通知内容与实际入库结果不一致。

        这里断言的是「提交前一次 dispatch 都没发生」—— 这是真正要守的行为。
        （回调列表本身在 pytest-django 的捕获夹具下不便断言，故不断言它。）
        """
        calls: list[str] = []
        monkeypatch.setattr(
            "apps.mailboxes.tasks.dispatch",
            lambda func, *a, **kw: (calls.append(func.__name__), ("queued", None))[1],
        )

        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        with django_capture_on_commit_callbacks(execute=False):
            with transaction.atomic():
                mode = enqueue_notify(EVENT_TICKET_CREATED, ticket)
            assert mode == "deferred"
            assert calls == []  # 关键：提交前不该入队

    def test_dispatches_callbacks_on_commit(
        self, tech_group, tech_mailbox, django_capture_on_commit_callbacks, monkeypatch
    ):
        calls: list[str] = []
        monkeypatch.setattr(
            "apps.mailboxes.tasks.dispatch",
            lambda func, *a, **kw: (calls.append(func.__name__), ("queued", None))[1],
        )
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        with django_capture_on_commit_callbacks(execute=True):
            with transaction.atomic():
                enqueue_notify(EVENT_TICKET_CREATED, ticket)
        assert calls == ["notify_event_task"]

    def test_never_raises(self, tech_group, tech_mailbox, monkeypatch, transactional_db):
        """挂在收信主链路上，任何意外都不能让一封信丢掉。

        用 `transactional_db`（真实事务、**不**被外层 atomic 包裹），
        这样 `enqueue_notify` 走的是「立即 dispatch」分支；
        否则在 pytest-django 默认的 atomic 包裹下会走 on_commit，
        异常被推迟到提交时，测不到返回值的失败路径。
        """

        def boom(*args, **kwargs):
            raise RuntimeError("入队炸了")

        monkeypatch.setattr("apps.mailboxes.tasks.dispatch", boom)
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert enqueue_notify(EVENT_TICKET_CREATED, ticket) == "failed"

    def test_dispatches_immediately_outside_transaction(
        self, tech_group, tech_mailbox, monkeypatch, transactional_db
    ):
        """不在事务中时不该有额外延迟（收信流水线/改派走的就是这条路径）。"""
        calls: list[str] = []
        monkeypatch.setattr(
            "apps.mailboxes.tasks.dispatch",
            lambda func, *a, **kw: (calls.append(func.__name__), ("queued", None))[1],
        )
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert enqueue_notify(EVENT_TICKET_CREATED, ticket) == "queued"
        assert calls == ["notify_event_task"]


class TestNotifyEventTask:
    def test_takes_ids_not_objects(self, tech_group, tech_user, tech_mailbox):
        """RQ 会把参数序列化进 Redis，所以只传 id。"""
        from apps.audit.models import Setting

        Setting.set("ntfy_enabled", "true")
        Setting.set("ntfy_server_url", "https://ntfy.example.com")

        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        stats = notify_event_task(EVENT_TICKET_CREATED, ticket.pk)
        assert stats["enabled"] is True
        assert stats["recipients"] == 1

    def test_missing_ticket_is_noop(self):
        assert notify_event_task(EVENT_TICKET_CREATED, 999999) == {}

    def test_resolves_actor_and_mentioned(self, tech_group, tech_user, tech_mailbox):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        stats = notify_event_task(
            EVENT_NOTE_MENTIONED, ticket.pk, tech_user.pk, [tech_user.pk]
        )
        # 发起人=被 @ 者，规则 1 + 认领人为空 → 无受众
        assert stats["recipients"] == 0
