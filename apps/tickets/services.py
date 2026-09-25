"""工单核心服务（开发文档 §5.1 / §5.5）。

归并优先级：References / In-Reply-To > 主题工单号，两者都必须校验发件人
与工单 `customer_email` 一致（§2.2 工单号校验）。
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record as audit
from apps.core.utils import (
    extract_email,  # noqa: F401  # 开发文档 §5.1 公开 API，其他模块从这里导入
    message_id_tokens,
    normalize_subject,  # noqa: F401
    ticket_number_from_subject,
)
from apps.tickets.models import Attachment, Message, Ticket

logger = logging.getLogger(__name__)


def normalize_message_id(value: str | None) -> str:
    """Message-ID 归一化：去空白与尖括号，便于跨邮件比对。"""
    return (value or "").strip().strip("<>").strip()


def find_ticket(msg, mailbox=None) -> Ticket | None:
    """按开发文档 §5.1 归并已有工单，找不到返回 None。

    1. References / In-Reply-To：命中 message_id 且发件人一致
    2. 主题中的 [T#123]：必须校验发件人一致，否则视为新工单
    """
    sender = extract_email(msg.get("From", ""))
    if not sender:
        return None

    raw_refs = f"{msg.get('References', '') or ''} {msg.get('In-Reply-To', '') or ''}"
    for token in message_id_tokens(raw_refs):
        candidate_id = normalize_message_id(token)
        if not candidate_id:
            continue
        message = (
            Message.objects.select_related("ticket")
            .filter(message_id=candidate_id)
            .order_by("-created_at", "-id")
            .first()
        )
        if message is None and candidate_id != token:
            message = (
                Message.objects.select_related("ticket")
                .filter(message_id=token)
                .order_by("-created_at", "-id")
                .first()
            )
        if message and message.ticket.customer_email == sender:
            return message.ticket

    number = ticket_number_from_subject(msg.get("Subject", ""))
    if number:
        ticket = Ticket.objects.filter(pk=number).first()
        if ticket and ticket.customer_email == sender:
            return ticket
        if ticket:
            # 发件人不一致：不归并，走新工单（可追溯）
            logger.info(
                "主题工单号 T#%s 的发件人 %s 与工单客户 %s 不一致，按新工单处理。",
                number,
                sender,
                ticket.customer_email,
            )

    return None


def create_ticket(
    *,
    mailbox,
    group,
    subject: str,
    customer_email: str,
    now=None,
) -> Ticket:
    """新建工单（主题同时写入归一化结果，§4.3 normalized_subject）。"""
    now = now or timezone.now()
    ticket = Ticket.objects.create(
        mailbox=mailbox,
        group=group,
        subject=subject or "(无主题)",
        normalized_subject=normalize_subject(subject),
        customer_email=customer_email,
        last_message_at=now,
    )
    audit(
        action="config_change",
        ticket=ticket,
        group=group,
        detail={
            "event": "ticket_created",
            "entry_mailbox": mailbox.email if mailbox else "",
            "customer": customer_email,
            "subject": ticket.subject,
        },
    )
    return ticket


# ------------------------------------------------------------------ 待回复标签（§5.5）
def on_inbound(ticket: Ticket, now=None) -> Ticket:
    """客户来信：打上待回复标签并刷新活跃时间。"""
    now = now or timezone.now()
    ticket.is_awaiting_reply = True
    ticket.last_message_at = now
    ticket.save(update_fields=["is_awaiting_reply", "last_message_at", "updated_at"])
    return ticket


def on_outbound(ticket: Ticket) -> Ticket:
    """客服外发回复：待回复标签消失。"""
    ticket.is_awaiting_reply = False
    ticket.save(update_fields=["is_awaiting_reply", "updated_at"])
    return ticket


# ------------------------------------------------------------------ 消息写入
@transaction.atomic
def store_inbound_message(
    *,
    ticket: Ticket,
    mailbox,
    parsed: dict,
    attachments: list[dict] | None = None,
    now=None,
) -> Message:
    """把解析后的入站邮件落库为 Message + Attachment。"""
    now = now or timezone.now()
    message = Message.objects.create(
        ticket=ticket,
        mailbox=mailbox,
        message_id=normalize_message_id(parsed.get("message_id")),
        in_reply_to=parsed.get("in_reply_to", "") or "",
        references=parsed.get("references", "") or "",
        direction="in",
        type="message",
        from_addr=parsed.get("from_addr", "") or "",
        to_addr=parsed.get("to_addr", "") or "",
        cc_addr=parsed.get("cc_addr", "") or "",
        subject=parsed.get("subject", "") or "",
        body_text=parsed.get("body_text", "") or "",
        body_html=parsed.get("body_html", "") or "",
        sent_at=parsed.get("sent_at") or now,
    )
    for attachment in attachments or []:
        Attachment.objects.create(message=message, **attachment)
    return message


@transaction.atomic
def add_note(ticket: Ticket, user, body_text: str, body_html: str = "") -> Message:
    """内部备注（§4.3 type=note）：组内可见，不对外发信，不影响待回复标签。"""
    return Message.objects.create(
        ticket=ticket,
        mailbox=ticket.mailbox,
        direction="out",
        type="note",
        from_addr="",
        to_addr="",
        subject=f"[T#{ticket.pk}] 内部备注",
        body_text=body_text,
        body_html=body_html,
        actual_sender=user,
        sent_at=timezone.now(),
    )


# ------------------------------------------------------------------ 认领（§2.6）
def claim_ticket(ticket: Ticket, user) -> Ticket:
    """认领工单：可选操作，认领后他人不再看到该工单的"待回复"提示。"""
    if ticket.assignee_id == user.pk:
        return ticket
    previous = ticket.assignee
    ticket.assignee = user
    ticket.save(update_fields=["assignee", "updated_at"])
    audit(
        action="claim",
        user=user,
        ticket=ticket,
        group=ticket.group,
        detail={"previous_assignee": previous.username if previous else ""},
    )
    return ticket


def unclaim_ticket(ticket: Ticket, user) -> Ticket:
    if ticket.assignee_id is None:
        return ticket
    if ticket.assignee_id != user.pk and not (user.sees_all_tickets):
        raise PermissionError("只有认领人本人或管理员可以取消认领。")
    previous = ticket.assignee
    ticket.assignee = None
    ticket.save(update_fields=["assignee", "updated_at"])
    audit(
        action="unclaim",
        user=user,
        ticket=ticket,
        group=ticket.group,
        detail={"previous_assignee": previous.username if previous else ""},
    )
    return ticket


# ------------------------------------------------------------------ 状态流转
def set_status(ticket: Ticket, status: str, user=None) -> Ticket:
    if status not in dict(Ticket.STATUS_CHOICES):
        raise ValueError(f"非法工单状态：{status}")
    if ticket.status == status:
        return ticket
    previous = ticket.status
    ticket.status = status
    ticket.save(update_fields=["status", "updated_at"])
    audit(
        action="config_change",
        user=user,
        ticket=ticket,
        group=ticket.group,
        detail={"event": "status_changed", "from": previous, "to": status},
    )
    return ticket


def touch_last_message(ticket: Ticket, when=None) -> Ticket:
    when = when or timezone.now()
    ticket.last_message_at = when
    ticket.save(update_fields=["last_message_at", "updated_at"])
    return ticket


def timeline(ticket: Ticket):
    """工单时间线：消息（含备注）+ 审计事件按时间排序。"""
    return ticket.messages.select_related("actual_sender", "mailbox").order_by("created_at", "id")
