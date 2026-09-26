"""通知受众解析测试（契约 §4.4，规则源自 `03-通知与推送设计.md` §1）。

重点覆盖：
- 四条事件各自的受众范围
- 三条全局规则（不推发起人 / 同一人不重复推 / 已关闭工单不推）
- 改派推的是**新组**（原组已失可见，推给原组是错的）
- 未认领全组广播、认领后只推认领人
"""

from __future__ import annotations

import pytest

from apps.notifications.audience import (
    EVENT_NOTE_MENTIONED,
    EVENT_TICKET_CREATED,
    EVENT_TICKET_INBOUND,
    EVENT_TICKET_REASSIGNED,
    group_members,
    resolve_recipients,
)
from tests.conftest import make_ticket

pytestmark = pytest.mark.django_db


def pks(users):
    return sorted(u.pk for u in users)


class TestGroupMembers:
    def test_returns_group_users(self, tech_group, tech_user):
        assert pks(group_members(tech_group)) == [tech_user.pk]

    def test_inactive_user_excluded(self, tech_group, tech_user, finance_user):
        from apps.accounts.models import UserGroup

        UserGroup.objects.create(user=finance_user, group=tech_group)
        assert pks(group_members(tech_group)) == [tech_user.pk, finance_user.pk]
        finance_user.is_active = False
        finance_user.save(update_fields=["is_active"])
        assert pks(group_members(tech_group)) == [tech_user.pk]

    def test_none_group_returns_empty(self):
        assert group_members(None) == []


class TestTicketCreated:
    def test_broadcasts_to_whole_group(self, tech_group, tech_user, tech_mailbox):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert pks(resolve_recipients(EVENT_TICKET_CREATED, ticket)) == [tech_user.pk]

    def test_does_not_include_other_groups(
        self, tech_group, tech_user, finance_user, tech_mailbox
    ):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        recipients = resolve_recipients(EVENT_TICKET_CREATED, ticket)
        assert finance_user.pk not in pks(recipients)


class TestInbound:
    def test_unassigned_broadcasts_to_group(self, tech_group, tech_user, tech_mailbox):
        """用户确认的语义：未认领全推。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert ticket.assignee_id is None
        assert pks(resolve_recipients(EVENT_TICKET_INBOUND, ticket)) == [tech_user.pk]

    def test_assigned_goes_only_to_assignee(
        self, tech_group, tech_user, tech_mailbox, finance_user
    ):
        """认领之后只推给认领的人 —— 与「认领后只有认领人看到待回复」同一套模型。"""
        from apps.accounts.models import UserGroup

        UserGroup.objects.create(user=finance_user, group=tech_group)
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group, assignee=tech_user)
        assert pks(resolve_recipients(EVENT_TICKET_INBOUND, ticket)) == [tech_user.pk]


class TestReassigned:
    def test_targets_new_group_not_old(
        self, tech_group, finance_group, tech_user, finance_user, tech_mailbox
    ):
        """改派是**组级**的，且原组已失去可见性 —— 推给原组是错的。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        ticket.group = finance_group
        ticket.assignee = None
        recipients = resolve_recipients(EVENT_TICKET_REASSIGNED, ticket)
        assert pks(recipients) == [finance_user.pk]
        assert tech_user.pk not in pks(recipients)


class TestNote:
    def test_mentioned_plus_assignee(
        self, tech_group, tech_user, tech_mailbox, admin_group_user
    ):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group, assignee=tech_user)
        recipients = resolve_recipients(
            EVENT_NOTE_MENTIONED, ticket, mentioned=[admin_group_user]
        )
        assert pks(recipients) == [tech_user.pk, admin_group_user.pk]

    def test_assignee_deduped_when_also_mentioned(
        self, tech_group, tech_user, tech_mailbox
    ):
        """规则 2：组广播与认领人重合时只推一条。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group, assignee=tech_user)
        recipients = resolve_recipients(
            EVENT_NOTE_MENTIONED, ticket, mentioned=[tech_user]
        )
        assert pks(recipients) == [tech_user.pk]

    def test_no_mention_no_assignee_is_empty(self, tech_group, tech_mailbox):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert resolve_recipients(EVENT_NOTE_MENTIONED, ticket) == []


class TestGlobalRules:
    def test_actor_is_never_notified(self, tech_group, tech_user, tech_mailbox):
        """规则 1：不通知动作发起人自己。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert resolve_recipients(EVENT_TICKET_CREATED, ticket, actor=tech_user) == []

    def test_closed_ticket_is_never_notified(self, tech_group, tech_user, tech_mailbox):
        """规则 3：已关闭工单不推（任何事件都不推）。"""
        ticket = make_ticket(
            mailbox=tech_mailbox, group=tech_group, status="closed"
        )
        for event in (
            EVENT_TICKET_CREATED,
            EVENT_TICKET_INBOUND,
            EVENT_TICKET_REASSIGNED,
            EVENT_NOTE_MENTIONED,
        ):
            assert resolve_recipients(event, ticket) == []

    def test_recipients_are_deduped(self, tech_group, tech_user, tech_mailbox):
        """规则 2：同一个人不会被推两次。

        这里让同一个人同时是「认领人」和「被 @ 者」，并重复出现在提及列表里。
        """
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group, assignee=tech_user)
        recipients = resolve_recipients(
            EVENT_NOTE_MENTIONED, ticket, mentioned=[tech_user, tech_user]
        )
        assert pks(recipients) == [tech_user.pk]

    def test_unknown_event_raises(self, tech_group, tech_mailbox):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        with pytest.raises(ValueError):
            resolve_recipients("no_such_event", ticket)

    def test_superadmin_is_not_implicitly_notified(
        self, tech_group, superadmin, tech_mailbox
    ):
        """超管能看见全部工单，但**不在组里就不该收到该组的每条通知**。

        否则超管会被全站噪音淹没 —— 受众是「组」不是「可见性」。
        """
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert superadmin.pk not in pks(resolve_recipients(EVENT_TICKET_CREATED, ticket))
