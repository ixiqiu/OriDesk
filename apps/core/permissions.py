"""权限装饰器与访问控制工具（开发文档 §2.7 / §10.3）。

所有视图都必须经过 `@login_required` 或这里的更强装饰器，
安全清单要求逐条落实（§10.3：所有视图有 @login_required 或 DRF 权限类）。
"""

from __future__ import annotations

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404

from apps.tickets.models import Ticket
from apps.tickets.selectors import can_manage_routing, visible_tickets

__all__ = [
    "login_required",
    "api_login_required",
    "superadmin_required",
    "routing_manager_required",
    "mailbox_admin_required",
    "get_visible_ticket",
    "can_manage_routing",
]


def api_login_required(view_func):
    """移动端 API 的登录门（契约 §2.2，决策 D8）。

    **为什么不能直接用 `login_required`**：Django 的 `login_required` 未登录时
    **302 跳登录页**。对浏览器是对的，对 App 是灾难 —— 客户端会把一整页 HTML
    当成 API 响应去解析，最终表现为「请求失败」这种最难定位的症状。

    因此 API 一律返回 401 + 统一错误体，让 App 能明确区分「会话过期」并跳登录页。
    """

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not getattr(request.user, "is_authenticated", False):
            return JsonResponse(
                {
                    "error": {
                        "code": "unauthorized",
                        "message": "登录状态已失效，请重新登录。",
                    }
                },
                status=401,
            )
        return view_func(request, *args, **kwargs)

    return _wrapped


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


def mailbox_admin_required(view_func):
    """邮箱配置（查看/新建/编辑/连通性检查）的权限门。

    允许：超级管理员、管理员组成员、组内管理员（即 `sees_all_tickets` 的那批人，
    与 `routing_manager_required` 同一集合）。2026-09 经人工确认从"仅超管"放开，
    让各业务组能自行维护组邮箱。

    风险提示（已在 docs/邮箱接入指南.md §7 记录）：能配邮箱 = 能看到/修改**所有**邮箱
    的连接参数并更新凭据（凭据本身仍不回显明文）。因此：

    - 邮箱的新建/编辑/变更凭据都会写审计（谁、何时、改了哪些字段）；
    - 用户/用户组的管理权限仍只属于超级管理员，两者已解耦；
    - 若将来需要"只能管本组邮箱"的细分权限，在这里收窄即可。
    """

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
