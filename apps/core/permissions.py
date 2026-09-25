"""权限装饰器与访问控制工具（开发文档 §2.7 / §10.3）。

所有视图都必须经过 `@login_required` 或这里的更强装饰器，
安全清单要求逐条落实（§10.3：所有视图有 @login_required 或 DRF 权限类）。
"""

from __future__ import annotations

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404

from apps.tickets.models import Ticket
from apps.tickets.selectors import can_manage_routing, visible_tickets

__all__ = [
    "login_required",
    "superadmin_required",
    "routing_manager_required",
    "get_visible_ticket",
    "can_manage_routing",
]


def superadmin_required(view_func):
    """仅超级管理员（用户组、邮箱、全局配置的维护入口）。"""

    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        if not getattr(request.user, "is_superadmin", False):
            raise PermissionDenied("需要超级管理员权限。")
        return view_func(request, *args, **kwargs)

    return _wrapped


def routing_manager_required(view_func):
    """规则 / 模板 / 系统设置：超级管理员或管理员组成员。"""

    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        if not can_manage_routing(request.user):
            raise PermissionDenied("需要管理员权限。")
        return view_func(request, *args, **kwargs)

    return _wrapped


def get_visible_ticket(user, pk: int) -> Ticket:
    """按可见性规则取工单，不可见则 404（不泄露存在性）。"""
    ticket = visible_tickets(user).select_related("group", "mailbox", "assignee").filter(pk=pk).first()
    if ticket is None:
        raise Http404("工单不存在或无权访问。")
    return ticket


def get_group_or_404(pk: int):
    from apps.accounts.models import Group

    return get_object_or_404(Group, pk=pk)
