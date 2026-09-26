"""通知编排与聚合测试（契约 §4.4）。

**全部打桩** `ntfy.publish`：这里测的是「该不该发、发给谁、发几条、文案是什么」，
HTTP 层由 `test_notifications_ntfy.py` 单独覆盖。
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.audit.models import Setting
from apps.notifications import ntfy
from apps.notifications.audience import (
    EVENT_NOTE_MENTIONED,
    EVENT_TICKET_CREATED,
    EVENT_TICKET_INBOUND,
)
from apps.notifications.models import Subscription
from apps.notifications.services import notify_event, publish_to_user
from tests.conftest import make_ticket

pytestmark = pytest.mark.django_db

SERVER = "https://ntfy.example.com"


@pytest.fixture
def sent(monkeypatch):
    """拦下真实 HTTP，记录每次发布调用的参数。"""
    calls: list[dict] = []

    def fake_publish(topic, **kwargs):
        calls.append({"topic": topic, **kwargs})
        return ntfy.PublishResult(ok=True, status=200)

    monkeypatch.setattr(ntfy, "publish", fake_publish)
    return calls


@pytest.fixture
def enabled():
    Setting.set("ntfy_enabled", "true")
    Setting.set("ntfy_server_url", SERVER)
    Setting.set("mobile_public_base_url", "https://desk.example.com")


@pytest.fixture
def device(tech_user):
    return Subscription.objects.create(user=tech_user, topic="topic-a", server=SERVER)


class TestGating:
    def test_disabled_does_not_publish(
        self, sent, device, tech_group, tech_mailbox
    ):
        """默认关：没配置就一个请求都不该发。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        stats = notify_event(EVENT_TICKET_CREATED, ticket)
        assert stats["enabled"] is False
        assert sent == []

    def test_closed_ticket_does_not_publish(
        self, sent, enabled, device, tech_group, tech_mailbox
    ):
        """规则 3：已关闭工单不推。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group, status="closed")
        stats = notify_event(EVENT_TICKET_CREATED, ticket)
        assert stats["recipients"] == 0
        assert sent == []

    def test_user_without_subscription_is_skipped(
        self, sent, enabled, tech_group, tech_mailbox, tech_user
    ):
        """有受众但没有注册设备 → 不该发起任何发布。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        stats = notify_event(EVENT_TICKET_CREATED, ticket)
        assert stats["recipients"] == 1
        assert stats["devices"] == 0
        assert sent == []

    def test_disabled_subscription_is_skipped(
        self, sent, enabled, tech_group, tech_mailbox, tech_user
    ):
        Subscription.objects.create(
            user=tech_user, topic="topic-off", server=SERVER, enabled=False
        )
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        notify_event(EVENT_TICKET_CREATED, ticket)
        assert sent == []


class TestFanout:
    def test_publishes_to_each_device(
        self, sent, enabled, device, tech_user, tech_group, tech_mailbox
    ):
        """契约 §3.3：每用户**每设备**一个 topic —— 只发第一个会让其他手机收不到。"""
        Subscription.objects.create(user=tech_user, topic="topic-b", server=SERVER)
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        stats = notify_event(EVENT_TICKET_CREATED, ticket)
        assert stats["devices"] == 2
        assert sorted(c["topic"] for c in sent) == ["topic-a", "topic-b"]

    def test_title_and_body_carry_no_email_body(
        self, sent, enabled, device, tech_group, tech_mailbox
    ):
        """契约 §4.3：推送**只放工单号与主题**，客服邮件正文绝不外发。"""
        ticket = make_ticket(
            mailbox=tech_mailbox, group=tech_group, subject="无法登录后台"
        )
        notify_event(EVENT_TICKET_CREATED, ticket)
        assert f"T#{ticket.pk}" in sent[0]["title"]
        assert sent[0]["message"] == "无法登录后台"


class TestAggregation:
    def test_two_events_same_ticket_collapse_to_one_count(
        self, sent, enabled, device, tech_group, tech_mailbox
    ):
        """契约 §4.4：60s 窗口内同一工单多事件 → 一条「收到 N 封新来信」。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        now = timezone.now()

        notify_event(EVENT_TICKET_INBOUND, ticket, now=now)
        assert sent[0]["title"] == f"T#{ticket.pk} 有新来信"

        notify_event(EVENT_TICKET_INBOUND, ticket, now=now)
        assert sent[1]["title"] == f"T#{ticket.pk} 收到 2 封新来信"

        notify_event(EVENT_TICKET_INBOUND, ticket, now=now)
        assert sent[2]["title"] == f"T#{ticket.pk} 收到 3 封新来信"

    def test_sequence_id_stable_within_window(
        self, sent, enabled, device, tech_group, tech_mailbox
    ):
        """同一窗口内 sequence id 必须相同 —— 客户端靠它**替换**而不是堆叠通知。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        now = timezone.now()
        notify_event(EVENT_TICKET_INBOUND, ticket, now=now)
        notify_event(EVENT_TICKET_INBOUND, ticket, now=now)
        assert sent[0]["sequence_id"] == sent[1]["sequence_id"]

    def test_new_window_gets_new_sequence(
        self, sent, enabled, device, tech_group, tech_mailbox
    ):
        """跨窗口必须换 sequence，否则新通知会去替换一个已经过期的窗口。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        base = timezone.now()
        notify_event(EVENT_TICKET_INBOUND, ticket, now=base)
        notify_event(EVENT_TICKET_INBOUND, ticket, now=base + timezone.timedelta(minutes=5))
        assert sent[0]["sequence_id"] != sent[1]["sequence_id"]
        assert sent[1]["title"] == f"T#{ticket.pk} 有新来信"  # 计数重置

    def test_sequence_id_is_ntfy_safe(
        self, sent, enabled, device, tech_group, tech_mailbox
    ):
        """sequence id 会进 HTTP header，必须落在 ntfy 允许的字符集内。"""
        import re

        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        notify_event(EVENT_TICKET_CREATED, ticket)
        assert re.fullmatch(r"[-_A-Za-z0-9]+", sent[0]["sequence_id"])

    def test_group_burst_collapses_to_one_summary(
        self, sent, enabled, device, tech_group, tech_mailbox
    ):
        """契约 §4.4：同一组多个新工单 → 一条「组内新增 N 个工单」。

        这正是"一次 IMAP 轮询涌入几十封"的场景，不聚合就是噪音炸弹。
        """
        now = timezone.now()
        first = make_ticket(mailbox=tech_mailbox, group=tech_group, subject="A")
        second = make_ticket(mailbox=tech_mailbox, group=tech_group, subject="B")
        notify_event(EVENT_TICKET_CREATED, first, now=now)
        notify_event(EVENT_TICKET_CREATED, second, now=now)
        assert sent[-1]["title"] == "组内新增 2 个工单"

    def test_aggregation_is_per_user(
        self, sent, enabled, device, tech_group, tech_mailbox, admin_group_user
    ):
        """聚合桶按用户隔离 —— 不能把别人的事件算进我的计数。"""
        from apps.accounts.models import UserGroup

        UserGroup.objects.create(user=admin_group_user, group=tech_group)
        Subscription.objects.create(
            user=admin_group_user, topic="topic-admin", server=SERVER
        )
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        now = timezone.now()
        notify_event(EVENT_TICKET_INBOUND, ticket, now=now)
        # 每个人第一次都应该是「有新来信」，而不是有人被算成 2
        for call in sent:
            assert call["title"] == f"T#{ticket.pk} 有新来信"


class TestClickTargets:
    def test_click_uses_scheme_and_actions_carry_web_fallback(
        self, sent, enabled, device, tech_group, tech_mailbox
    ):
        """决策 D3：scheme 直接唤起 App；ntfy 的 Click 只能放一个 URL，
        所以 Web 兜底挂在 action 按钮上。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        notify_event(EVENT_TICKET_CREATED, ticket)
        assert sent[0]["click"] == f"oridesk://ticket/{ticket.pk}"
        assert "在浏览器打开" in sent[0]["actions"]
        assert f"https://desk.example.com/tickets/{ticket.pk}/" in sent[0]["actions"]

    def test_no_web_action_when_base_url_absent(
        self, sent, device, tech_group, tech_mailbox
    ):
        Setting.set("ntfy_enabled", "true")
        Setting.set("ntfy_server_url", SERVER)
        Setting.set("mobile_public_base_url", "")
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        notify_event(EVENT_TICKET_CREATED, ticket)
        assert sent[0]["click"] == f"oridesk://ticket/{ticket.pk}"
        assert sent[0]["actions"] == ""


class TestResilience:
    def test_publish_failure_is_counted_not_raised(
        self, enabled, device, tech_group, tech_mailbox, monkeypatch
    ):
        """推送失败绝不能冒泡到收信流水线。"""
        monkeypatch.setattr(
            ntfy,
            "publish",
            lambda topic, **kw: ntfy.PublishResult(ok=False, error="boom"),
        )
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        stats = notify_event(EVENT_TICKET_CREATED, ticket)
        assert stats["failed"] == 1
        assert stats["published"] == 0

    def test_unexpected_exception_is_swallowed(
        self, enabled, device, tech_group, tech_mailbox, monkeypatch
    ):
        def boom(topic, **kw):
            raise RuntimeError("意外")

        monkeypatch.setattr(ntfy, "publish", boom)
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        stats = notify_event(EVENT_TICKET_CREATED, ticket)
        assert stats["published"] == 0  # 没抛出去，调用方拿到的是统计

    def test_none_ticket_is_noop(self, sent, enabled):
        assert notify_event(EVENT_TICKET_CREATED, None)["enabled"] is False


class TestMentionEvent:
    def test_mention_title(
        self, sent, enabled, device, tech_user, tech_group, tech_mailbox
    ):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group, assignee=tech_user)
        notify_event(EVENT_NOTE_MENTIONED, ticket)
        assert sent[0]["title"] == f"T#{ticket.pk} 有人提到了你"


class TestPublishToUser:
    def test_sends_to_all_enabled_devices(self, sent, enabled, tech_user):
        Subscription.objects.create(user=tech_user, topic="d1", server=SERVER)
        Subscription.objects.create(user=tech_user, topic="d2", server=SERVER)
        Subscription.objects.create(
            user=tech_user, topic="d3", server=SERVER, enabled=False
        )
        assert publish_to_user(tech_user, title="测试", message="内容") == 2
        assert sorted(c["topic"] for c in sent) == ["d1", "d2"]
