"""审计写入服务（开发文档 §2.5：谁、何时、以哪个组身份、给哪个客户、发了什么）。"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from django.db import DatabaseError

logger = logging.getLogger(__name__)

_current_user: ContextVar[object | None] = ContextVar("audit_current_user", default=None)

_SENSITIVE_KEYS = {"secret", "password", "token", "authorization", "secret_encrypted"}


def set_current_user(user) -> None:
    """由中间件在请求开始时设置当前操作者。"""
    _current_user.set(user if getattr(user, "is_authenticated", False) else None)


def get_current_user():
    return _current_user.get()


def sanitize_detail(detail: dict | None) -> dict:
    """审计明细脱敏：永不写入任何凭据明文。"""
    if not detail:
        return {}
    clean: dict = {}
    for key, value in detail.items():
        if key.lower() in _SENSITIVE_KEYS:
            clean[key] = "***"
            continue
        if isinstance(value, str):
            clean[key] = value if len(value) <= 500 else value[:497] + "…"
        elif isinstance(value, (int, float, bool)) or value is None:
            clean[key] = value
        elif isinstance(value, (list, tuple)):
            clean[key] = [str(item)[:200] for item in value][:50]
        elif isinstance(value, dict):
            clean[key] = {str(k)[:100]: str(v)[:200] for k, v in list(value.items())[:50]}
        else:
            clean[key] = str(value)[:500]
    return clean


def record(
    *,
    action: str,
    user=None,
    ticket=None,
    group=None,
    identity_email: str = "",
    detail: dict | None = None,
    request=None,
):
    """写一条审计日志。失败只记录日志，不影响主流程。"""
    from apps.audit.models import AuditLog

    actor = user
    if actor is None and request is not None:
        candidate = getattr(request, "user", None)
        actor = candidate if getattr(candidate, "is_authenticated", False) else None
    if actor is None:
        actor = get_current_user()
    if actor is not None and not getattr(actor, "pk", None):
        actor = None

    if not identity_email and group is not None:
        identity_email = group.identity_email or ""

    try:
        return AuditLog.objects.create(
            user=actor,
            action=action,
            ticket=ticket,
            group=group,
            identity_email=identity_email or "",
            detail=sanitize_detail(detail),
        )
    except DatabaseError:  # pragma: no cover - 审计失败不应中断业务
        logger.exception("写入审计日志失败 action=%s ticket=%s", action, getattr(ticket, "pk", None))
        return None


def for_ticket(ticket, *, limit: int = 50):
    """工单时间线里的审计记录。"""
    return ticket.audit_logs.select_related("user", "group")[:limit]
