"""SMTP 发信（开发文档 §5.4 / §2.5 / §6.3）。

- 对外 `From` 与 `Reply-To` 都用组邮箱；无邮箱组走全局兜底邮箱。
- 内部记录实际发件人（messages.actual_sender），对外身份由 mailbox 决定。
- 主题带 `[T#123]`，并写 In-Reply-To / References 便于客户端线程归并。
- 发信成功后写审计（谁、何时、以哪个组身份、给哪个客户、发了什么）。
"""

from __future__ import annotations

import logging
import mimetypes
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from django.conf import settings
from django.utils import timezone

from apps.audit.services import record as audit
from apps.core.crypto import CredentialError
from apps.tickets.models import Attachment, Message, Ticket

logger = logging.getLogger(__name__)


class MailDeliveryError(Exception):
    """发信失败。"""


def normalize_message_id(value: str | None) -> str:
    return (value or "").strip().strip("<>").strip()


def guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    mime, _ = mimetypes.guess_type(filename or "")
    return mime or fallback


def build_mime(
    *,
    from_addr: str,
    to_addr: str,
    subject: str,
    body_text: str = "",
    body_html: str = "",
    reply_to: str = "",
    cc: list[str] | str | None = None,
    attachments: list | None = None,
    in_reply_to: str = "",
    references: str = "",
    headers: dict | None = None,
) -> EmailMessage:
    """构造 MIME 邮件。

    attachments 支持三种元素：{"filename","content"/"path","mime"} 字典、
    Django UploadedFile（有 read()/name/content_type）、或 Attachment 实例。
    """
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr if isinstance(to_addr, str) else ", ".join(to_addr)
    if reply_to:
        msg["Reply-To"] = reply_to
    if cc:
        msg["Cc"] = cc if isinstance(cc, str) else ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(from_addr.split("@")[-1] or None))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    for key, value in (headers or {}).items():
        if value is not None:
            msg[key] = str(value)

    msg.set_content(body_text or "")
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    for item in attachments or []:
        filename, content, mime = _read_attachment(item)
        if not content:
            continue
        maintype, _, subtype = (mime or guess_mime(filename)).partition("/")
        msg.add_attachment(
            content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=filename,
        )
    return msg


def _read_attachment(item) -> tuple[str, bytes, str]:
    """把各种形态的附件统一成 (filename, content, mime)。"""
    if isinstance(item, dict):
        filename = item.get("filename") or item.get("name") or "attachment"
        mime = item.get("mime") or guess_mime(filename)
        content = item.get("content")
        if content is None and item.get("path"):
            from apps.mailboxes import storage

            content = storage.read_file(item["path"])
        return filename, content or b"", mime

    if isinstance(item, Attachment):
        from apps.mailboxes import storage

        try:
            content = storage.read_file(item.path)
        except (OSError, ValueError):
            logger.warning("读取附件失败，已跳过：%s", item.path)
            content = b""
        return item.filename, content, item.mime or guess_mime(item.filename)

    # Django UploadedFile / 类文件对象
    filename = getattr(item, "name", None) or "attachment"
    mime = getattr(item, "content_type", "") or guess_mime(filename)
    if hasattr(item, "read"):
        try:
            item.seek(0)
        except (AttributeError, ValueError):
            pass
        return filename, item.read(), mime
    return filename, b"", mime


def smtp_send(mailbox, msg: EmailMessage, *, timeout: int = 30) -> None:
    """通过标准 SMTP 发信（隐式 TLS 或 STARTTLS），任何服务商通用。

    这是唯一的网络发信出口，测试中可 patch `apps.mailboxes.services.smtp_send`。
    凭据只在这里解密，绝不写日志。
    """
    try:
        secret = mailbox.get_secret()
    except CredentialError as exc:
        raise MailDeliveryError(f"邮箱 {mailbox.email} 凭据不可用：{exc}") from exc

    try:
        if mailbox.smtp_ssl:
            server = smtplib.SMTP_SSL(mailbox.smtp_host, mailbox.smtp_port, timeout=timeout)
        else:
            server = smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=timeout)
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls()
                server.ehlo()
    except (OSError, smtplib.SMTPException) as exc:
        raise MailDeliveryError(f"连接 SMTP 服务器失败：{mailbox.smtp_host}:{mailbox.smtp_port}") from exc

    try:
        server.login(mailbox.username, secret)
        server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailDeliveryError(f"SMTP 发信失败（{mailbox.email}）：{exc}") from exc
    finally:
        try:
            server.quit()
        except (OSError, smtplib.SMTPException):  # pragma: no cover
            try:
                server.close()
            except Exception:  # noqa: BLE001 - 关闭失败无需处理
                pass


def reply_subject(ticket: Ticket) -> str:
    """回信主题永远带工单号（§2.5 主题带 [T#123]，作为归并 fallback）。"""
    base = (ticket.subject or "(无主题)").strip()
    if base.startswith(f"[T#{ticket.pk}]"):
        return base
    return f"[T#{ticket.pk}] {base}"


def identity_mailbox_for(ticket: Ticket):
    """组邮箱优先，无邮箱组走全局兜底邮箱（§2.1 / §5.4）。"""
    mailbox = ticket.group.identity_mailbox if ticket.group_id else None
    if mailbox is None:
        raise MailDeliveryError(
            f"组「{ticket.group.name if ticket.group_id else '-'}」没有对外邮箱，且系统未配置全局兜底邮箱。"
        )
    return mailbox


def send_reply(
    ticket: Ticket,
    user,
    body_text: str,
    body_html: str = "",
    attachments: list | None = None,
    cc: list[str] | str | None = None,
    now=None,
) -> Message:
    """以组身份对外发信并落库（开发文档 §5.4）。"""
    now = now or timezone.now()
    group = ticket.group
    mailbox = identity_mailbox_for(ticket)

    msg = build_mime(
        from_addr=mailbox.email,
        reply_to=mailbox.email,
        to_addr=ticket.customer_email,
        subject=reply_subject(ticket),
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        cc=cc,
        in_reply_to=ticket.last_message_id,
        references=ticket.references_header,
    )
    smtp_send(mailbox, msg)

    message = Message.objects.create(
        ticket=ticket,
        mailbox=mailbox,
        message_id=normalize_message_id(msg.get("Message-ID", "")),
        in_reply_to=msg.get("In-Reply-To", "") or "",
        references=msg.get("References", "") or "",
        direction="out",
        type="message",
        from_addr=mailbox.email,
        to_addr=ticket.customer_email,
        cc_addr=msg.get("Cc", "") or "",
        subject=msg.get("Subject", ""),
        body_text=body_text,
        body_html=body_html,
        actual_sender=user,
        sent_at=now,
    )
    _store_outbound_attachments(message, ticket, msg, attachments)

    # 待回复标签消失 + 活跃时间刷新（§5.5）
    ticket.is_awaiting_reply = False
    ticket.last_message_at = now
    ticket.save(update_fields=["is_awaiting_reply", "last_message_at", "updated_at"])

    audit(
        user=user,
        action="reply",
        ticket=ticket,
        group=group,
        identity_email=mailbox.email,
        detail={
            "to": ticket.customer_email,
            "subject": ticket.subject,
            "attachments": [a.filename for a in message.attachments.all()],
        },
    )
    return message


def _store_outbound_attachments(message: Message, ticket: Ticket, msg: EmailMessage, attachments) -> None:
    """把已发出邮件的附件落盘并建 Attachment 记录（§6.3 附件收发）。"""
    from apps.mailboxes import storage

    if not attachments:
        return
    message_key = normalize_message_id(msg.get("Message-ID", "")) or f"message-{message.pk}"
    for item in attachments:
        filename, content, mime = _read_attachment(item)
        if not content:
            continue
        rel_path = storage.attachment_rel_path(ticket.pk, message_key, filename)
        try:
            storage.save_file(rel_path, content)
        except (OSError, ValueError):  # pragma: no cover
            logger.warning("保存外发附件失败：%s", filename)
            continue
        Attachment.objects.create(
            message=message,
            filename=filename,
            mime=mime,
            size=len(content),
            path=rel_path,
        )


def max_upload_size_bytes() -> int:
    from apps.mailboxes.storage import max_attachment_bytes

    return max_attachment_bytes()


def mail_enabled() -> bool:
    """总开关：本地开发/测试可在 .env 中关闭真实发信。"""
    return getattr(settings, "MAIL_SENDING_ENABLED", True)
