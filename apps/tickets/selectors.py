"""工单可见性查询（开发文档 §5.6 / §2.7）。"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.tickets.models import Ticket


def visible_tickets(user) -> QuerySet[Ticket]:
    """用户可见的工单集合。

    - 超级管理员：全部
    - 管理员组身份 / 组内管理员：全部（可跨组查看）
    - 普通用户：自己所在组的工单
    """
    if not getattr(user, "is_authenticated", False):
        return Ticket.objects.none()

    if user.is_superadmin:
        return Ticket.objects.all()

    if user.is_group_admin or user.is_admin_group_member:
        return Ticket.objects.all()

    group_ids = user.user_groups.values_list("group_id", flat=True)
    return Ticket.objects.filter(group_id__in=group_ids)


def visible_ticket_or_none(user, ticket_id: int) -> Ticket | None:
    return visible_tickets(user).filter(pk=ticket_id).first()


def can_manage_routing(user) -> bool:
    """规则/模板/设置的维护权限：超级管理员或任一管理员组成员。"""
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_superadmin or user.is_admin_group_member or user.is_group_admin)


def scope_counts(user) -> dict[str, int]:
    """列表页 chips 上的计数（**实现逐字搬自 `views._scope_counts`**，决策 D5）。

    口径必须与 `views.inbox()` 里各 scope 的过滤条件**逐字一致**，否则会出现
    "chip 显示 3 条、点进去只有 2 条"这种自相矛盾的界面。

    搬到 selectors 是因为**移动端角标端点也要用同一份口径**（`/api/mobile/badge/`）。
    若在那边另写一套统计，就是在复制这个被注释警告过的 bug。`views._scope_counts`
    现在只是转发到本函数，行为完全不变。
    """
    visible = visible_tickets(user)
    return {
        "all": visible.count(),
        "mine": visible.filter(assignee=user).count(),
        "unassigned": visible.filter(assignee__isnull=True).exclude(status="closed").count(),
        # 与 inbox() 的 awaiting scope 口径一致（同样排除已关闭），否则 chips 上的数字
        # 会和点进去的结果对不上
        "awaiting": visible.filter(is_awaiting_reply=True)
        .exclude(status="closed")
        .filter(Q(assignee__isnull=True) | Q(assignee=user))
        .count(),
    }


def pending_for_user(user) -> QuerySet[Ticket]:
    """当前用户的"待回复"队列（§2.6：认领后只有认领人看到待回复）。"""
    return (
        visible_tickets(user)
        .filter(is_awaiting_reply=True)
        # 已关闭工单不进待回复队列（§5.5 语义："等客户回信"对已关闭工单不成立）
        .exclude(status="closed")
        .filter(Q(assignee__isnull=True) | Q(assignee=user))
    )


def claimable_for_user(user) -> QuerySet[Ticket]:
    """当前用户可认领（未被认领或认领人是自己）。"""
    return visible_tickets(user).filter(Q(assignee__isnull=True) | Q(assignee=user))
