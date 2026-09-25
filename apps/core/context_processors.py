"""模板上下文：导航所需的组列表、待回复计数与权限标记。

匿名请求不查库，避免为登录页增加无谓开销。
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import Q


def navigation(request):
    base = {
        "nav_groups": [],
        "nav_awaiting_total": 0,
        "can_manage": False,
        "can_manage_users": False,
        "app_version": "1.1",
        "site_name": getattr(settings, "SITE_NAME", "新技元工单"),
    }
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return base

    from apps.accounts.models import Group, UserGroup
    from apps.core.permissions import can_manage_routing
    from apps.tickets.selectors import pending_for_user

    if user.sees_all_tickets:
        groups = Group.objects.all().order_by("name")
    else:
        groups = Group.objects.filter(user_groups__user=user).distinct().order_by("name")

    base.update(
        {
            "nav_groups": groups,
            "nav_awaiting_total": pending_for_user(user).count(),
            "nav_my_memberships": UserGroup.objects.filter(user=user).select_related("group"),
            "can_manage": can_manage_routing(user),
            "can_manage_users": bool(user.is_superadmin),
            "my_group_ids": list(user.user_groups.values_list("group_id", flat=True)),
            "unassigned_count": _unassigned_count(user),
        }
    )
    return base


def _unassigned_count(user) -> int:
    from apps.tickets.selectors import visible_tickets

    return visible_tickets(user).filter(assignee__isnull=True).filter(
        Q(status="open") | Q(status="pending")
    ).count()
