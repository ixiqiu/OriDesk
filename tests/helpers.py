"""构造测试用邮件与对象的公共工具。

放在 tests/helpers.py 而不是各测试文件里，保证 §12 验收用例的输入形态一致。
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def build_email(
    *,
    sender: str = "customer@customer-domain.com",
    to: str = "support@example.com",
    subject: str = "无法登录后台",
    body: str = "你好，我这边登录一直失败。",
    message_id: str | None = None,
    references: str = "",
    in_reply_to: str = "",
    headers: dict | None = None,
    html: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id or make_msgid(domain="customer-domain.com")
    if references:
        msg["References"] = references
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return msg


def build_raw(**kwargs) -> bytes:
    return build_email(**kwargs).as_bytes()


def message_id_of(msg: EmailMessage) -> str:
    return str(msg["Message-ID"]).strip().strip("<>")
