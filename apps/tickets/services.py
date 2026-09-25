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


# ------------------------------------------------------------------ 标签（§4.4 add_tag 落库）
def tags_available_for(group):
    """某工单可用的标签：全局标签 + 该组专属标签（只含启用中的），返回 Tag 查询集。"""
    from django.db.models import Q

    from apps.tickets.models import Tag

    queryset = Tag.objects.filter(is_active=True)
    if group is None:
        return queryset.filter(group__isnull=True).order_by("name")
    return queryset.filter(Q(group__isnull=True) | Q(group=group)).order_by("name")


def resolve_tag(name: str, *, group=None, create: bool = True):
    """按名称解析标签：优先该组专属标签，其次全局标签；不存在且 create 时新建。

    - 名称比较用 `iexact`，与数据库的 utf8mb4_unicode_ci 语义一致（避免大小写重复标签）；
    - 新建的标签落在 group 作用域（group 为 None 则建全局标签），符合"规则由管理员维护"的语义；
    - 只解析**启用中**的标签；全部停用时视为不存在（可新建同名启用标签？不——见下）。
    """
    from django.db import IntegrityError

    from apps.tickets.models import Tag

    normalized = Tag.normalize_name(name)
    if not normalized:
        return None

    candidates = Tag.objects.filter(name__iexact=normalized)
    if group is not None:
        tag = candidates.filter(group=group).first()
        if tag:
            return tag
    tag = candidates.filter(group__isnull=True).first()
    if tag:
        return tag
    if not create:
        return None
    if candidates.exists():
        # 同名标签存在但已停用：不新建（否则会撞唯一约束），由调用方按"不可用"处理
        return None
    try:
        return Tag.objects.create(name=normalized, group=group)
    except IntegrityError:  # pragma: no cover - 并发下重复创建
        return Tag.objects.filter(name=normalized, group=group).first()


def find_tag(name: str, *, group=None):
    """按名称查标签（不限启用状态与归属作用域），返回最匹配的一个。

    匹配顺序：本组专属 > 全局 > 其他组同名（用于给出准确的错误提示）。
    与 `resolve_tag` 的区别：本函数**永不创建**，也不会因为"同名但不可用"而返回 None。
    """
    from apps.tickets.models import Tag

    normalized = Tag.normalize_name(name)
    if not normalized:
        return None
    candidates = list(Tag.objects.filter(name__iexact=normalized).select_related("group"))
    if not candidates:
        return None
    if group is not None:
        for tag in candidates:
            if tag.group_id == group.pk:
                return tag
    for tag in candidates:
        if tag.group_id is None:
            return tag
    return candidates[0]


def is_tag_available_for(tag, group) -> bool:
    """标签是否可用于该组的工单（全局标签人人可用，组标签仅限本组，且必须启用）。"""
    return tag.is_available_for(group)


def add_tag(ticket, tag_or_name, user=None, source: str = "manual", *, record_audit: bool = True):
    """给工单打标签（幂等）。返回 (TicketTag, created)。

    执行顺序（重要）：
      1. 参数归一化 → 空名直接拒绝；
      2. **查找**标签（不创建）：命中则校验"已停用 / 属于别的组"并给出准确提示；
      3. 未命中 → 这是全新标签：**先查单工单数量上限**，通过后才创建 Tag；
      4. 已存在关联 → 幂等返回（不占新额度、不写审计）；否则建关联。

    这样即使因为"超过上限"被拒，也不会残留一条没有任何工单引用的孤立标签。
    """
    from apps.tickets.models import MAX_TAGS_PER_TICKET, Tag, TicketTag

    if isinstance(tag_or_name, Tag):
        tag = tag_or_name
    else:
        normalized = Tag.normalize_name(tag_or_name)
        if not normalized:
            raise ValueError("标签名不能为空。")
        tag = find_tag(normalized, group=ticket.group)

    def _count() -> int:
        return TicketTag.objects.filter(ticket=ticket).count()

    def _assert_quota() -> None:
        if _count() >= MAX_TAGS_PER_TICKET:
            raise ValueError(
                f"单个工单最多 {MAX_TAGS_PER_TICKET} 个标签，请先移除不再需要的标签。"
            )

    if tag is not None:
        if not tag.is_active:
            raise ValueError(f"标签「{tag.name}」已停用，无法使用；如需恢复请在标签管理里启用。")
        if not is_tag_available_for(tag, ticket.group):
            raise ValueError(
                f"标签「{tag.name}」属于用户组「{tag.scope_label}」，不能用于本工单。"
            )
        existing = TicketTag.objects.filter(ticket=ticket, tag=tag).first()
        if existing is not None:
            return existing, False
        _assert_quota()
    else:
        # 全新标签：先卡上限，再落库，避免留下孤立 Tag
        _assert_quota()
        tag = resolve_tag(tag_or_name, group=ticket.group, create=True)
        if tag is None:
            raise ValueError("标签名不能为空、或存在同名但已停用的标签。")

    link = TicketTag.objects.create(
        ticket=ticket,
        tag=tag,
        added_by=user if source == "manual" else None,
        source=source,
    )
    if record_audit:
        audit(
            user=user,
            action="config_change",
            ticket=ticket,
            group=ticket.group,
            detail={"event": "tag_added", "tag": tag.name, "source": source},
        )
    return link, True


def remove_tag(ticket, tag, user=None, *, record_audit: bool = True) -> bool:
    """移除工单标签，返回是否确有删除。"""
    from apps.tickets.models import Tag, TicketTag

    if isinstance(tag, str):
        tag = TicketTag.objects.filter(ticket=ticket, tag__name=Tag.normalize_name(tag)).first()
        if tag is None:
            return False
        tag = tag.tag
    deleted, _ = TicketTag.objects.filter(ticket=ticket, tag=tag).delete()
    if deleted and record_audit:
        audit(
            user=user,
            action="config_change",
            ticket=ticket,
            group=ticket.group,
            detail={"event": "tag_removed", "tag": tag.name},
        )
    return bool(deleted)


def ticket_tag_names(ticket) -> list[str]:
    return list(
        ticket.ticket_tags.select_related("tag").values_list("tag__name", flat=True)
    )
