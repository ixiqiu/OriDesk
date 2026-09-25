"""路由引擎（开发文档 §5.2 / §2.2 / §2.3）。

优先级：粘性 > 规则（按 priority，第一个命中即停）> 兜底组 > 管理员组。

补充说明（文档未直接写明、由 §2.2 表格推导，见 docs/需求实现对照.md）：
- 收件邮箱若已绑定用户组（组专用邮箱），来信**直接进入该组**，不做主题路由；
  但若能归并到已有工单，则归并原工单（归并判断在 pipeline 中先行）。
- 管理员组邮箱同样按"绑定组的邮箱"处理。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import Group, Mailbox
from apps.audit.models import Setting
from apps.audit.services import record as audit
from apps.routing.models import Rule
from apps.tickets.models import Ticket

logger = logging.getLogger(__name__)


class RoutingError(Exception):
    """无法为工单找到归属组（例如系统未配置任何组）。"""


def sticky_window_days() -> int:
    return Setting.get_int("sticky_window_days", 7)


def first_contact_window_hours() -> int:
    return Setting.get_int("first_contact_window_hours", 24)


def fallback_group() -> Group | None:
    """§4.6 fallback_group_id 指定的兜底组。"""
    group_id = Setting.get_optional_int("fallback_group_id")
    if group_id:
        group = Group.objects.filter(pk=group_id).first()
        if group:
            return group
    return None


def admin_group() -> Group | None:
    """管理员组（§2.7：管理员组是权限容器，也是无兜底时的归宿）。"""
    return Group.objects.filter(is_admin_group=True).order_by("id").first()


def recent_ticket_for_sender(sender: str, now=None) -> Ticket | None:
    """同发件人最近一条工单，用于粘性判断（§2.3 以 last_message_at 为基准）。"""
    return (
        Ticket.objects.select_related("group")
        .filter(customer_email=sender)
        .order_by("-last_message_at", "-id")
        .first()
    )


def match_rule(msg, mailbox) -> Rule | None:
    """按优先级匹配规则，第一个命中即停（§2.2 多规则命中）。"""
    rules = (
        Rule.objects.filter(mailbox=mailbox, enabled=True)
        .select_related("mailbox")
        .order_by("priority", "id")
    )
    for rule in rules:
        if rule.matches(msg):
            return rule
    return None


def route_ticket(msg, sender: str, mailbox, now=None) -> Group:
    """返回应归属的 Group（开发文档 §5.2）。"""
    now = now or timezone.now()

    # 0. 组专用邮箱：直接进入该组（§2.2 发到组邮箱，不能归并 → 直接进入该组）
    entry_group = getattr(mailbox, "owning_group", None) if mailbox is not None else None
    if entry_group is not None:
        return entry_group

    # 1. 短期粘性：同发件人 N 天内再次进线，归原组
    window_days = sticky_window_days()
    last = recent_ticket_for_sender(sender, now=now)
    if last and last.last_message_at and last.last_message_at >= now - timedelta(days=window_days):
        return last.group

    # 2. 规则匹配
    rule = match_rule(msg, mailbox) if mailbox is not None else None
    if rule is not None:
        group = rule.resolve_group()
        if group:
            return group
        logger.warning("规则 id=%s 命中但未能解析出目标组，继续后续路由。", rule.pk)

    # 3. 兜底组
    group = fallback_group()
    if group:
        return group

    # 4. 管理员组
    group = admin_group()
    if group:
        return group

    raise RoutingError(
        "无可用归属组：请配置兜底组（fallback_group_id）或至少一个管理员组。"
    )


def apply_rule_actions(ticket: Ticket, msg, mailbox, user=None) -> list[str]:
    """执行命中规则的副作用动作（§4.4 ACTION_TYPE_CHOICES）。

    - set_status：直接改工单状态
    - assign_user：把工单认领给指定用户（仅当其属于该工单的组或拥有跨组权限时）
    - add_tag：v1.1 数据模型没有标签字段，按"可追溯"原则写入审计 detail
      （真正实现多标签需要人工确认新增模型，见文档 §10.4）
    """
    rule = match_rule(msg, mailbox) if mailbox is not None else None
    if rule is None:
        return []

    applied: list[str] = []
    if rule.action_type == "set_status":
        value = (rule.action_value or "").strip()
        if value in dict(Ticket.STATUS_CHOICES):
            ticket.status = value
            ticket.save(update_fields=["status", "updated_at"])
            applied.append(f"status={value}")
        else:
            logger.warning("规则 id=%s 的 set_status 值非法：%s", rule.pk, value)

    elif rule.action_type == "assign_user":
        target = rule.resolved_user()
        if target is not None:
            ticket.assignee = target
            ticket.save(update_fields=["assignee", "updated_at"])
            applied.append(f"assignee={target.username}")
        else:
            logger.warning("规则 id=%s 的 assign_user 未找到用户：%s", rule.pk, rule.action_value)

    elif rule.action_type == "add_tag":
        applied.append(f"tag={rule.action_value}")

    if applied:
        audit(
            user=user,
            action="config_change",
            ticket=ticket,
            group=ticket.group,
            detail={"event": "rule_action", "rule_id": rule.pk, "applied": applied},
        )
    return applied


def reassign_ticket(ticket: Ticket, target_group: Group, user, reason: str = "") -> Ticket:
    """改派到任意组（§2.2 改派）。

    - 原组默认失去可见性（超管/管理员组不受影响，由可见性规则天然保证）
    - 认领人一并清空（原组人员不应继续持有该工单）
    - 写入审计
    """
    if target_group is None:
        raise ValueError("改派目标组不能为空。")
    if ticket.group_id == target_group.pk:
        raise ValueError("改派目标组与当前归属组相同。")

    source_group = ticket.group
    previous_assignee = ticket.assignee
    ticket.group = target_group
    ticket.assignee = None
    ticket.save(update_fields=["group", "assignee", "updated_at"])

    audit(
        user=user,
        action="forward",
        ticket=ticket,
        group=target_group,
        identity_email=source_group.identity_email or "",
        detail={
            "from_group": source_group.name,
            "to_group": target_group.name,
            "reason": reason,
            "previous_assignee": previous_assignee.username if previous_assignee else "",
        },
    )
    logger.info(
        "工单 T#%s 由 %s 改派至 %s（操作人 %s）",
        ticket.pk,
        source_group.name,
        target_group.name,
        getattr(user, "username", "-"),
    )
    return ticket


def reassignable_groups(ticket: Ticket):
    """改派下拉框可选组：任意组（§2.2）。"""
    return Group.objects.exclude(pk=ticket.group_id).order_by("name")


def entry_kind(mailbox: Mailbox) -> str:
    """邮箱类型：group（组专用）/ fallback（全局兜底）/ unified（统一进线）。"""
    if getattr(mailbox, "owning_group", None) is not None:
        return "group"
    if mailbox.is_fallback:
        return "fallback"
    return "unified"
