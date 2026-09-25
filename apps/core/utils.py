"""公共工具：邮箱地址解析、正文提取、主题归正化、时间解析。

`extract_email` / `normalize_subject` 的实现以开发文档 §5.1 / §5.7 为准。
"""

from __future__ import annotations

import re
from datetime import datetime
from datetime import timezone as dt_timezone
from email.header import decode_header, make_header
from email.message import Message as EmailMessage
from email.utils import parsedate_to_datetime

_TICKET_NO_RE = re.compile(r"\[T#(\d+)\]")
_PREFIX_RE = re.compile(r"^\s*((re|fwd|fw|回复|转发|答复)\s*[:：]\s*)+", re.IGNORECASE)


def extract_email(header_value: str) -> str:
    """从 '张三 <a@b.com>' 提取 a@b.com（开发文档 §5.1）。"""
    match = re.search(r"<([^>]+)>", header_value or "")
    if match:
        return match.group(1).strip().lower()
    return (header_value or "").strip().lower()


def extract_emails(header_value: str) -> list[str]:
    """从 'a@b.com, 张三 <c@d.com>' 提取全部地址，保序去重。"""
    if not header_value:
        return []
    raw = decode_mime_header(header_value)
    found = re.findall(r"<([^>]+)>", raw)
    if not found:
        found = [part for part in re.split(r"[,;\s]+", raw) if "@" in part]
    result: list[str] = []
    for item in found:
        addr = item.strip().strip("<>").strip().strip(",").lower()
        if addr and addr not in result:
            result.append(addr)
    return result


def normalize_subject(subject: str) -> str:
    """剥离 Re:/Fwd:/回复:/转发: 等前缀，并去掉工单号（开发文档 §5.7）。"""
    s = decode_mime_header(subject or "")
    prev = None
    while prev != s:
        prev = s
        s = _PREFIX_RE.sub("", s)
    s = _TICKET_NO_RE.sub("", s)
    return s.strip()


def ticket_number_from_subject(subject: str) -> int | None:
    """从主题中提取 [T#123] 中的工单号。"""
    match = _TICKET_NO_RE.search(decode_mime_header(subject or ""))
    return int(match.group(1)) if match else None


def decode_mime_header(value: str | None) -> str:
    """解码 =?utf-8?b?...?= 形式的邮件头。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError, ValueError):
        return value


def parse_date(value: str | None) -> datetime | None:
    """解析邮件 Date 头；无时区的按 UTC 处理（USE_TZ=True）。"""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def message_id_tokens(raw: str | None) -> list[str]:
    """把 References / In-Reply-To 头切成一个个 message-id。"""
    if not raw:
        return []
    return [token for token in decode_mime_header(raw).split() if token]


def plain_text_from_message(msg: EmailMessage) -> str:
    """从 email.message.Message 里取 text/plain 正文（不做复杂解码）。"""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except LookupError:
                    return payload.decode("utf-8", errors="replace")
        return ""
    if msg.get_content_type() == "text/plain":
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    return ""


def html_to_text(html: str) -> str:
    """HTML 降级为纯文本（用于正文预览与规则匹配）。"""
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate(value: str, length: int) -> str:
    return value if len(value) <= length else value[: length - 1] + "…"
