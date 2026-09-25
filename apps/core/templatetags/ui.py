"""模板标签：查询串拼接、角标、权限判断等视图无关的小工具。"""

from __future__ import annotations

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

_STATUS_CLASS = {
    "open": "badge-open",
    "pending": "badge-pending",
    "closed": "badge-closed",
}


@register.simple_tag(takes_context=True)
def qs_replace(context, **kwargs):
    """在当前查询串上替换若干参数，用于筛选器与分页链接。"""
    request = context.get("request")
    params = request.GET.copy() if request is not None else {}
    for key, value in kwargs.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = value
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else "?"


@register.filter
def status_badge(ticket):
    css = _STATUS_CLASS.get(getattr(ticket, "status", ""), "badge-closed")
    return format_html('<span class="badge {}">{}</span>', css, ticket.get_status_display())


@register.filter
def awaiting_badge(ticket):
    if not getattr(ticket, "is_awaiting_reply", False):
        return mark_safe("")
    if getattr(ticket, "assignee_id", None):
        return format_html('<span class="badge badge-awaiting">待回复 · {}</span>', ticket.assignee.display_name)
    return format_html('<span class="badge badge-awaiting">待回复</span>')


@register.filter
def direction_label(message):
    if message.type == "note":
        return "内部备注"
    return "收信" if message.direction == "in" else "发信"


@register.filter
def sender_label(message):
    if message.type == "note":
        return message.actual_sender.display_name if message.actual_sender else "系统"
    if message.direction == "in":
        return message.from_addr
    if message.actual_sender:
        return f"{message.actual_sender.display_name}（以 {message.from_addr} 发出）"
    return message.from_addr or "系统"


@register.filter
def can_see_contact(user, ticket):
    """内部联系方式可见性：同组或跨组权限。"""
    if not getattr(user, "is_authenticated", False):
        return False
    if user.sees_all_tickets:
        return True
    return ticket.group_id in user.group_ids


@register.simple_tag(takes_context=True)
def nav_active(context, *url_names):
    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    if match is None:
        return ""
    current = match.view_name or ""
    for name in url_names:
        if current == name or current.startswith(name + ":"):
            return "active"
    return ""
