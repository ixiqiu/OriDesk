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


def pending_for_user(user) -> QuerySet[Ticket]:
    """当前用户的"待回复"队列（§2.6：认领后只有认领人看到待回复）。"""
    return (
        visible_tickets(user)
        .filter(is_awaiting_reply=True)
        .filter(Q(assignee__isnull=True) | Q(assignee=user))
    )


def claimable_for_user(user) -> QuerySet[Ticket]:
    """当前用户可认领（未被认领或认领人是自己）。"""
    return visible_tickets(user).filter(Q(assignee__isnull=True) | Q(assignee=user))
