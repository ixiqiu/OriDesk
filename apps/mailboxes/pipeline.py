"""入站邮件处理主流程（开发文档 §6.5）。

与文档伪代码的两处必要修正（已在 docs/需求实现对照.md 记录）：
1. `should_auto_reply` 必须用**归并结果**（归并到已有工单则为该工单，否则 None）判断，
   并且要在新建工单**之前**求值，否则永远判定为"非首次进线"。
2. 附件先解析到内存，等工单与消息落库后再按
   `media/attachments/{ticket_id}/{message_id}/{filename}` 落盘（§6.3）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Mailbox
from apps.autoresponder.services import should_auto_reply
from apps.core.utils import extract_email
from apps.mailboxes import parser, storage
from apps.mailboxes.filters import is_loop_mail, loop_reason
from apps.mailboxes.sanitizer import sanitize_html
from apps.routing.services import apply_rule_actions, route_ticket
from apps.tickets.models import Message, Ticket
from apps.tickets.services import (
    create_ticket,
    find_ticket,
    on_inbound,
    store_inbound_message,
)

logger = logging.getLogger(__name__)


@dataclass
class InboundResult:
    status: str  # processed / skipped_loop / skipped_duplicate
    ticket: Ticket | None = None
    message: Message | None = None
    created: bool = False
    auto_replied: bool = False
    rule_actions: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def processed(self) -> bool:
        return self.status == "processed"


def already_seen(mailbox: Mailbox, message_id: str) -> bool:
    """幂等保护：同一个 Message-ID 在同一入口邮箱只处理一次。

    可防止 IMAP 重投、UIDVALIDITY 重置后重扫导致的重复工单。
    注意：入库时 Message-ID 已做归一化（去尖括号），这里必须同样归一化再比对。
    """
    key = (message_id or "").strip().strip("<>").strip()
    if not key:
        return False
    return Message.objects.filter(
        mailbox=mailbox, message_id=key, direction="in"
    ).exists()


def process_inbound(mailbox: Mailbox, uid: int, raw_bytes: bytes, now=None) -> InboundResult:
    """单封邮件的处理流水线（开发文档 §6.5）。"""
    now = now or timezone.now()
    msg = parser.parse_mime(raw_bytes)

    # 1. 防循环（§6.1）
    if is_loop_mail(msg):
        reason = loop_reason(msg) or "loop"
        logger.info("邮箱 %s UID=%s 命中防循环规则（%s），已丢弃。", mailbox.email, uid, reason)
        return InboundResult(status="skipped_loop", reason=reason)

    # 2. 解析
    parsed = parser.parse_inbound(raw_bytes)
    sender = parsed.get("from_addr") or extract_email(msg.get("From", ""))
    if not sender:
        logger.warning("邮箱 %s UID=%s 无法解析发件人，已丢弃。", mailbox.email, uid)
        return InboundResult(status="skipped_loop", reason="no_sender")

    if already_seen(mailbox, parsed.get("message_id", "")):
        logger.info("邮箱 %s UID=%s 的邮件已处理过，跳过。", mailbox.email, uid)
        return InboundResult(status="skipped_duplicate", reason="duplicate_message_id")

    parsed["body_html"] = sanitize_html(parsed.get("body_html") or "")
    attachments = parsed.pop("attachments", []) or []

    # 3. 归并（§5.1）；同时判定是否首次进线（必须早于建单）
    merged = find_ticket(msg, mailbox)
    is_first_contact = should_auto_reply(merged, sender, now=now)

    # 4. 新建或归并
    created = False
    if merged is None:
        group = route_ticket(msg, sender, mailbox, now=now)
        ticket = create_ticket(
            mailbox=mailbox,
            group=group,
            subject=parsed.get("subject", ""),
            customer_email=sender,
            now=now,
        )
        created = True
    else:
        # 归并原工单，保持原组，不重新路由（§2.2 / §2.3）
        ticket = merged

    with transaction.atomic():
        # 5. 写消息 + 附件落盘
        message = store_inbound_message(
            ticket=ticket, mailbox=mailbox, parsed=parsed, now=now
        )
        _persist_attachments(ticket, message, attachments)

        # 6. 待回复标签（§5.5）
        on_inbound(ticket, now=now)

    # 7. 规则副作用动作（set_status / assign_user / add_tag）
    rule_actions: list[str] = []
    if created:
        try:
            rule_actions = apply_rule_actions(ticket, msg, mailbox)
        except Exception:  # noqa: BLE001 - 规则动作失败不应丢信
            logger.exception("工单 T#%s 的规则动作执行失败。", ticket.pk)

    # 8. 自动回复（§5.3）
    auto_replied = False
    if is_first_contact:
        from apps.autoresponder.services import send_auto_reply

        try:
            auto_message = send_auto_reply(ticket, mailbox, now=now)
            auto_replied = auto_message is not None
        except Exception:  # noqa: BLE001 - 自动回复失败不影响工单入库
            logger.exception("工单 T#%s 自动回复发送失败。", ticket.pk)

    # 9. 移动端推送（契约 §4.4）—— 必须异步，且绝不能影响收信。
    # 这是收信流水线的最后一步，位次即 `02-OriDesk后端侦察.md` §2.1 指明的钩子点
    # （`return InboundResult(...)` 之前）。`enqueue_notify` 自身不抛异常，这里再包
    # 一层是照抄本函数既有的防御风格（见上面 apply_rule_actions / 自动回复两处）：
    # 通知失败绝不丢信。
    # 位次也在 `with transaction.atomic()` 之外，所以入队时工单/消息都已提交。
    # 入站邮件没有"动作发起人"（发件人是客户、不是系统用户），故 actor=None。
    try:
        from apps.notifications.audience import (
            EVENT_TICKET_CREATED,
            EVENT_TICKET_INBOUND,
        )
        from apps.notifications.tasks import enqueue_notify

        enqueue_notify(
            EVENT_TICKET_CREATED if created else EVENT_TICKET_INBOUND,
            ticket,
            subject=parsed.get("subject", ""),
        )
    except Exception:  # noqa: BLE001 - 通知失败不能影响收信
        logger.exception("工单 T#%s 的通知派发失败。", ticket.pk)

    return InboundResult(
        status="processed",
        ticket=ticket,
        message=message,
        created=created,
        auto_replied=auto_replied,
        rule_actions=rule_actions,
    )


def _persist_attachments(ticket: Ticket, message: Message, attachments: list[dict]) -> int:
    """按 §6.3 的路径规范把内存态附件落盘并建记录。"""
    from apps.tickets.models import Attachment

    if not attachments:
        return 0
    message_key = message.message_id or f"message-{message.pk}"
    stored = 0
    for item in attachments:
        record = storage.store_inbound_attachment(
            ticket_id=ticket.pk,
            message_key=message_key,
            filename=item.get("filename") or "attachment",
            content=item.get("content") or b"",
            mime=item.get("mime") or "",
        )
        if record is None:
            continue
        Attachment.objects.create(message=message, **record)
        stored += 1
    return stored
