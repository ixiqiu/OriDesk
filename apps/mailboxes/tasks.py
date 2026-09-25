"""RQ 异步任务（开发文档 §8 / §9：worker 负责发信、附件与 IMAP 同步）。

所有任务都是"薄封装"：真正的逻辑仍在 services / sync / pipeline 中，
任务函数只负责按 id 取对象并调用，便于单测直接调用业务函数。

`dispatch()` 是统一派发入口：Redis 可用则入队，否则同步执行（本地开发友好）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def redis_available() -> bool:
    try:
        from django_rq import get_queue

        get_queue("mail").connection.ping()
        return True
    except Exception:  # noqa: BLE001 - 任何连接问题都视为不可用
        return False


def dispatch(func, *args, queue: str = "mail", **kwargs):
    """把任务函数派发到 RQ；Redis 不可用时同步执行。

    返回 (mode, result)，mode 为 "queued" 或 "inline"。
    """
    try:
        from django_rq import get_queue

        get_queue(queue).enqueue(func, *args, **kwargs)
        return "queued", None
    except Exception as exc:  # noqa: BLE001
        logger.warning("RQ 不可用（%s），改为同步执行 %s。", exc, getattr(func, "__name__", func))
        return "inline", func(*args, **kwargs)


# ------------------------------------------------------------------ 发信
def send_reply_task(
    ticket_id: int,
    user_id: int | None,
    body_text: str,
    body_html: str = "",
    attachments: list | None = None,
    cc=None,
):
    """异步发信任务（附件以内存字典形式传入，内容为 bytes）。"""
    from django.contrib.auth import get_user_model

    from apps.mailboxes.services import send_reply
    from apps.tickets.models import Ticket

    ticket = Ticket.objects.get(pk=ticket_id)
    user = get_user_model().objects.filter(pk=user_id).first() if user_id else None
    return send_reply(
        ticket,
        user,
        body_text,
        body_html=body_html,
        attachments=attachments,
        cc=cc,
    ).pk


# ------------------------------------------------------------------ IMAP
def sync_mailbox_task(mailbox_id: int, limit: int | None = None) -> dict:
    from apps.accounts.models import Mailbox
    from apps.mailboxes.sync import sync_mailbox

    mailbox = Mailbox.objects.filter(pk=mailbox_id, is_active=True).first()
    if mailbox is None:
        return {"mailbox": mailbox_id, "error": "mailbox not found or inactive"}
    return sync_mailbox(mailbox, limit=limit)


def sync_all_task(limit: int | None = None) -> list[dict]:
    from apps.mailboxes.sync import sync_all_mailboxes

    return sync_all_mailboxes(limit=limit)


def process_inbound_task(mailbox_id: int, uid: int, raw_bytes: bytes) -> dict:
    from apps.accounts.models import Mailbox
    from apps.mailboxes.pipeline import process_inbound

    mailbox = Mailbox.objects.get(pk=mailbox_id)
    result = process_inbound(mailbox, uid, raw_bytes)
    return {
        "status": result.status,
        "ticket_id": result.ticket.pk if result.ticket else None,
        "created": result.created,
        "auto_replied": result.auto_replied,
    }
