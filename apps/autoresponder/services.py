"""自动回复（开发文档 §5.3 / §2.4 / §7-4）。

要点：
- 首次进线判断：未归并到任何已有工单，且同发件人 N 小时内无新建工单。
- 模板解析：组覆盖 > 全局 > 代码内置默认模板。
- 自动回复本身写入 messages 并标记 is_auto_reply=True，且带
  `Auto-Submitted: auto-replied` 形成对称防循环（§6.1）。
- 自动回复**不**清除"待回复"标签：那是人工回复才做的事（§2.6）。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.template import Context, Engine, TemplateSyntaxError
from django.utils import timezone

from apps.audit.models import Setting
from apps.core.utils import extract_email
from apps.mailboxes import services as mail_services
from apps.routing.models import Template
from apps.tickets.models import Message, Ticket

logger = logging.getLogger(__name__)

# 代码内置兜底模板：数据库中没有任何模板时也能工作（可通过管理界面覆盖）
DEFAULT_TEMPLATE = """您好：

我们已收到您的来信，工单号为 [T#{{ ticket_id }}]，我们会尽快安排同事跟进。

为便于后续回复自动归并到同一工单，请保留邮件标题中的 [T#{{ ticket_id }}]。

—— {{ group_name }}"""

TEMPLATE_VARIABLES = [
    "ticket_id",
    "ticket_no",
    "subject",
    "customer_email",
    "group_name",
    "identity_email",
    "date",
]

_engine = Engine(autoescape=False, string_if_invalid="")


def render_template(body: str, context: dict) -> str:
    """渲染 {{ 变量 }} 形式的模板，语法错误时原样返回（不阻断发信）。"""
    if not body:
        return ""
    try:
        return _engine.from_string(body).render(Context(context, autoescape=False))
    except TemplateSyntaxError:
        logger.warning("自动回复模板语法错误，已按原文发送。")
        return body


def template_context(ticket: Ticket, mailbox) -> dict:
    group = ticket.group
    return {
        "ticket_id": ticket.pk,
        "ticket_no": f"[T#{ticket.pk}]",
        "subject": ticket.subject,
        "customer_email": ticket.customer_email,
        "group_name": group.name if group else "",
        "identity_email": mailbox.email if mailbox else "",
        "date": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
    }


def resolve_template(mailbox) -> Template | None:
    """模板解析：组覆盖 > 全局（开发文档 §5.3）。"""
    group = getattr(mailbox, "owning_group", None) if mailbox is not None else None
    if group is not None:
        tpl = Template.objects.filter(scope="group", group=group).order_by("id").first()
        if tpl:
            return tpl
    return Template.objects.filter(scope="global").order_by("id").first()


def resolve_template_body(mailbox) -> str:
    tpl = resolve_template(mailbox)
    return tpl.body if tpl else DEFAULT_TEMPLATE


def should_auto_reply(merged_ticket: Ticket | None, sender: str, now=None) -> bool:
    """首次进线判断（开发文档 §5.3）。

    参数 `merged_ticket` 必须是**归并结果**：归并到已有工单时为该工单，否则为 None。
    注意：调用方必须在新建工单**之前**调用本函数，否则新工单会把自己判定为"窗口内已有工单"。
    """
    if merged_ticket is not None:
        return False

    now = now or timezone.now()
    window_hours = Setting.get_int("first_contact_window_hours", 24)
    return not Ticket.objects.filter(
        customer_email=sender,
        created_at__gte=now - timedelta(hours=window_hours),
    ).exists()


def auto_reply_already_sent(ticket: Ticket) -> bool:
    """防重复（§7-4）：同一工单最多一条自动回复。"""
    return Message.objects.filter(ticket=ticket, is_auto_reply=True).exists()


def render_auto_reply_body(ticket: Ticket, mailbox, body: str | None = None) -> str:
    """渲染最终要发送的自动回复正文。"""
    text = resolve_template_body(mailbox) if body is None else body
    return render_template(text, template_context(ticket, mailbox))


def build_auto_reply(ticket: Ticket, mailbox, rendered_body: str):
    """构造自动回复 MIME 邮件（供发送与测试复用）。"""
    return mail_services.build_mime(
        from_addr=mailbox.email,
        reply_to=mailbox.email,
        to_addr=ticket.customer_email,
        subject=f"[T#{ticket.pk}] {ticket.subject}",
        body_text=rendered_body,
        headers={
            "Auto-Submitted": "auto-replied",
            "X-Auto-Response-Suppress": "All",
            "Precedence": "auto_reply",
        },
    )


def send_auto_reply(ticket: Ticket, mailbox, now=None) -> Message | None:
    """发送并记录自动回复（开发文档 §5.3）。

    返回写入的 Message；被防重复拦截时返回 None。
    """
    if mailbox is None:
        logger.warning("工单 T#%s 无可用发信邮箱，跳过自动回复。", ticket.pk)
        return None
    if auto_reply_already_sent(ticket):
        logger.info("工单 T#%s 已发送过自动回复，跳过。", ticket.pk)
        return None

    now = now or timezone.now()
    body_text = render_auto_reply_body(ticket, mailbox)
    msg = build_auto_reply(ticket, mailbox, body_text)
    mail_services.smtp_send(mailbox, msg)

    return Message.objects.create(
        ticket=ticket,
        mailbox=mailbox,
        message_id=mail_services.normalize_message_id(msg.get("Message-ID", "")),
        direction="out",
        type="message",
        from_addr=mailbox.email,
        to_addr=ticket.customer_email,
        subject=msg["Subject"],
        body_text=body_text,
        is_auto_reply=True,
        sent_at=now,
    )


def auto_reply_sender(msg) -> str:
    return extract_email(msg.get("From", ""))
