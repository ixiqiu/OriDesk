"""MIME 解析层（开发文档 §6.3、§6.5）。

对外接口（已冻结，pipeline / sync 按此调用）：

- :func:`parse_mime` —— 字节流 → ``email.message.Message``。
- :func:`extract_headers` —— 头字段（Message-ID / In-Reply-To / References /
  From / To / Cc / Subject / Date），全部 RFC2047 解码后的 ``str``。
- :func:`extract_body` —— ``(body_text, body_html)``，只有 HTML 时用
  :func:`apps.core.utils.html_to_text` 生成纯文本兜底。
- :func:`extract_attachments` —— 内存态附件列表。
- :func:`parse_inbound` —— 综合入口。

实现约定：

- 使用 ``email.policy.compat32`` 解析，保证 ``msg.get("...")`` 返回原始字符串、
  ``msg.walk()`` / ``part.get_payload(decode=True)`` 语义与文档示例一致。
- 正文编码按 ``part.get_content_charset()`` 解码，未知/非法编码回退
  ``utf-8`` 且 ``errors="replace"``，绝不抛出。
- 有 ``get_filename()`` 的 part 视为附件，不参与正文提取。
"""

from __future__ import annotations

import email
import email.policy
import logging
import re
from email.header import decode_header, make_header
from email.message import Message

from apps.core.utils import (
    decode_mime_header,
    extract_email,
    html_to_text,
    parse_date,
)

logger = logging.getLogger(__name__)

#: extract_headers 返回的键 → 原始头名（开发文档 §6.5）。
_HEADER_FIELDS = {
    "message_id": "Message-ID",
    "in_reply_to": "In-Reply-To",
    "references": "References",
    "from": "From",
    "to": "To",
    "cc": "Cc",
    "subject": "Subject",
    "date": "Date",
}

#: 视为「正文」而不是附件的内容类型。
_BODY_TYPES = ("text/plain", "text/html")

#: 附件大小配置项（开发文档 §4.6）。
_MAX_ATTACHMENT_SIZE_KEY = "max_attachment_size_mb"
_MAX_ATTACHMENT_SIZE_DEFAULT_MB = 25

#: 生成附件名时使用的扩展名映射。
_EXT_ALIASES = {
    "jpeg": "jpg",
    "plain": "txt",
    "x-png": "png",
    "x-msdownload": "exe",
    "calendar": "ics",
    "vcard": "vcf",
    "x-vcard": "vcf",
}
_EXT_RE = re.compile(r"^[a-z0-9]{1,6}$")
_UNSAFE_FILENAME_RE = re.compile(r"[\x00-\x1f\x7f]")


# --------------------------------------------------------------- 解析入口
def parse_mime(raw_bytes: bytes) -> Message:
    """把原始邮件字节流解析成 ``email.message.Message``（开发文档 §6.5）。

    使用 ``email.policy.compat32``，与文档中 ``msg.get(...)`` / ``msg.walk()`` /
    ``part.get_payload(decode=True)`` 的用法完全兼容。

    :param raw_bytes: 原始 RFC822 字节流。
    :return: 解析后的邮件对象。
    """
    return email.message_from_bytes(raw_bytes or b"", policy=email.policy.compat32)


def _repair_surrogates(value: str) -> str:
    """修复未按 RFC2047 编码的原始 UTF-8 邮件头。

    ``compat32`` 对非 ASCII 字节使用 ``surrogateescape`` 解码，直接
    ``make_header`` 会得到乱码；这里先把 surrogate 还原成原始字节再按 UTF-8
    解码，兼容部分客户端直接发原始 UTF-8 头的情况。
    """
    if not any("\ud800" <= ch <= "\udfff" for ch in value):
        return value
    try:
        return value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except (UnicodeEncodeError, UnicodeDecodeError):  # pragma: no cover - 防御性
        return value


def _chunk_to_text(chunk, charset) -> str:
    """把 ``decode_header`` 返回的一个 chunk（str 或 bytes）转成文本。"""
    if isinstance(chunk, str):
        return _repair_surrogates(chunk)
    encoding = charset or "ascii"
    try:
        return chunk.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return chunk.decode("utf-8", errors="replace")


def _clean_header_value(value) -> str:
    """RFC2047 解码 + 折行空白归一（开发文档 §6.5 要求「去空白」）。

    ``compat32`` 遇到非 ASCII 原始字节时 ``get()`` 会返回
    ``email.header.Header``；``decode_header`` 支持该类型，逐 chunk 处理后
    再拼接，避免直接 ``str()`` 变成乱码。
    """
    if value is None:
        return ""
    if hasattr(value, "_chunks"):  # email.header.Header
        decoded = "".join(_chunk_to_text(c, cs) for c, cs in decode_header(value))
    else:
        decoded = decode_mime_header(_repair_surrogates(str(value)))
    return re.sub(r"\s+", " ", decoded).strip()


def extract_headers(msg) -> dict:
    """提取并解码关键头字段（开发文档 §6.5）。

    :param msg: :func:`parse_mime` 的返回值。
    :return: ``{"message_id", "in_reply_to", "references", "from", "to",
        "cc", "subject", "date"}``，全部为 ``str``，缺失时为 ``""``。
    """
    return {key: _clean_header_value(msg.get(name, "")) for key, name in _HEADER_FIELDS.items()}


# ------------------------------------------------------------------- 正文
def _is_attachment_part(part: Message) -> bool:
    """判断一个 part 是否应视为附件（不参与正文提取，见 §6.3）。"""
    if part.get_filename():
        return True
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "attachment":
        return True

    content_type = part.get_content_type()
    if part.get_content_maintype() == "multipart":
        return False
    if content_type.startswith("message/"):
        # 无文件名的 message/rfc822 视为被转发的正文，继续向内解析
        return False
    if content_type in ("text/calendar", "text/vcard", "text/x-vcard"):
        # 会议邀请 / 名片：不是正文，按附件保留
        return True
    if content_type in _BODY_TYPES:
        return False
    if disposition == "inline":
        return True
    return part.get_content_maintype() in (
        "image", "audio", "video", "application", "font", "model",
    )


def _iter_body_parts(part: Message, *, is_root: bool = True):
    """深度优先遍历出所有非附件叶子 part，附件容器整体跳过。"""
    if not is_root and _is_attachment_part(part):
        return
    if part.is_multipart():
        for child in part.get_payload():
            if isinstance(child, Message):
                yield from _iter_body_parts(child, is_root=False)
        return
    yield part


def _decode_part(part: Message) -> str:
    """按 part 声明的字符集解码正文；未知/非法编码回退 utf-8 replace。"""
    payload = part.get_payload(decode=True)
    if payload is None:
        # 未编码（7bit/8bit 的 compat32 情况）时 payload 是 str
        raw = part.get_payload()
        if isinstance(raw, str):
            return _repair_surrogates(raw)
        return ""
    if not isinstance(payload, (bytes, bytearray)):
        return str(payload)

    charset = part.get_content_charset()
    if charset:
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    return payload.decode("utf-8", errors="replace")


def extract_body(msg) -> tuple[str, str]:
    """提取正文 ``(body_text, body_html)``（开发文档 §6.3 / §6.5）。

    - 处理嵌套 ``multipart/*`` 与 ``message/rfc822``；
    - 跳过带 ``get_filename()`` 的附件 part；
    - 同一类型出现多份时取第一份非空正文；
    - 只有 HTML 时用 :func:`apps.core.utils.html_to_text` 生成纯文本兜底。

    :param msg: :func:`parse_mime` 的返回值。
    :return: ``(纯文本, HTML)``，缺失的一方为 ``""``。
    """
    body_text = ""
    body_html = ""
    html_fallback = ""
    for part in _iter_body_parts(msg):
        content_type = part.get_content_type()
        if content_type == "text/html":
            html_fallback = html_fallback or _decode_part(part)
            if not body_html:
                body_html = html_fallback
        elif content_type == "text/plain" and not body_text:
            body_text = _decode_part(part)

    if not body_text and body_html:
        body_text = html_to_text(body_html)
    # 去掉 MIME 分隔/CRLF 带来的首尾空白，便于入库与规则匹配
    return body_text.strip(), body_html.strip()


# ------------------------------------------------------------------- 附件
def _default_max_size_bytes() -> int:
    """从 ``Setting.get_int("max_attachment_size_mb", 25)`` 计算上限（§6.3）。

    配置值非法（``None``/非数字）时回退 25MB；显式配置为 ``0`` 表示不允许
    任何附件（返回 0，全部跳过）。
    """
    from apps.audit.models import Setting

    mb = Setting.get_int(_MAX_ATTACHMENT_SIZE_KEY, _MAX_ATTACHMENT_SIZE_DEFAULT_MB)
    try:
        mb = int(mb)
    except (TypeError, ValueError):
        mb = _MAX_ATTACHMENT_SIZE_DEFAULT_MB
    if mb < 0:
        mb = _MAX_ATTACHMENT_SIZE_DEFAULT_MB
    return mb * 1024 * 1024


def _generated_filename(part: Message, index: int) -> str:
    """为无文件名 part 生成名字，如 ``image001.png``（开发文档 §6.3）。"""
    maintype = part.get_content_maintype() or "application"
    subtype = (part.get_content_subtype() or "bin").lower()
    ext = _EXT_ALIASES.get(subtype, subtype if _EXT_RE.match(subtype) else "bin")
    stem = "image" if maintype == "image" else "attachment"
    return f"{stem}{index:03d}.{ext}"


def _attachment_filename(part: Message, index: int) -> str:
    """RFC2047 解码文件名并去掉路径/控制字符；无名字则自动生成。"""
    raw = part.get_filename()
    name = ""
    if raw:
        try:
            name = str(make_header(decode_header(raw)))
        except (LookupError, UnicodeDecodeError, ValueError):
            name = str(raw)
    name = _UNSAFE_FILENAME_RE.sub("", name).replace("\\", "/").split("/")[-1].strip()
    if not name or name in (".", ".."):
        return _generated_filename(part, index)
    return name


def _attachment_bytes(part: Message) -> bytes | None:
    """取出附件字节；``message/rfc822`` 这类 multipart 附件序列化内层邮件。"""
    payload = part.get_payload(decode=True)
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if part.is_multipart():
        chunks: list[bytes] = []
        for child in part.get_payload():
            if isinstance(child, Message):
                try:
                    chunks.append(child.as_bytes())
                except (TypeError, ValueError):  # pragma: no cover - 防御性
                    continue
        return b"".join(chunks)
    raw = part.get_payload()
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="replace")
    return None


def _is_inline_part(part: Message) -> bool:
    """内联附件判定：``Content-Disposition: inline`` 或带 ``Content-ID``。"""
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "inline":
        return True
    if disposition == "attachment":
        return False
    return bool(part.get("Content-ID"))


def extract_attachments(msg, *, max_size_bytes: int | None = None) -> list[dict]:
    """提取内存态附件列表（开发文档 §6.3）。

    :param msg: :func:`parse_mime` 的返回值。
    :param max_size_bytes: 单附件大小上限；``None`` 时取
        ``Setting.get_int("max_attachment_size_mb", 25)``（单位 MB）折算。
    :return: ``[{"filename", "mime", "size", "content", "inline"}, ...]``；
        超限附件被跳过并记 ``logging.warning``，不抛异常。
    """
    if max_size_bytes is None:
        max_size_bytes = _default_max_size_bytes()

    attachments: list[dict] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" and not part.get_filename():
            continue
        if not _is_attachment_part(part):
            continue

        content = _attachment_bytes(part)
        if content is None:
            continue

        size = len(content)
        if max_size_bytes is not None and size > max_size_bytes:
            logger.warning(
                "附件超限已跳过：filename=%s size=%s limit=%s",
                _attachment_filename(part, len(attachments) + 1),
                size,
                max_size_bytes,
            )
            continue

        attachments.append(
            {
                "filename": _attachment_filename(part, len(attachments) + 1),
                "mime": part.get_content_type(),
                "size": size,
                "content": content,
                "inline": _is_inline_part(part),
            }
        )
    return attachments


# --------------------------------------------------------------- 综合入口
def parse_inbound(raw_bytes: bytes) -> dict:
    """入站邮件综合解析（开发文档 §6.5 pipeline 第 2 步）。

    :param raw_bytes: 原始 RFC822 字节流。
    :return: 包含 ``headers``、``message_id``、``in_reply_to``、``references``、
        ``from_addr``、``to_addr``、``cc_addr``、``subject``、``sent_at``、
        ``body_text``、``body_html``、``attachments`` 的字典。
        ``from_addr`` 经 :func:`apps.core.utils.extract_email` 归一化；
        ``to_addr`` / ``cc_addr`` 保留（已解码的）原始头字符串。
    """
    msg = parse_mime(raw_bytes)
    headers = extract_headers(msg)
    body_text, body_html = extract_body(msg)
    return {
        "headers": headers,
        "message_id": headers["message_id"],
        "in_reply_to": headers["in_reply_to"],
        "references": headers["references"],
        "from_addr": extract_email(headers["from"]),
        "to_addr": headers["to"],
        "cc_addr": headers["cc"],
        "subject": headers["subject"],
        "sent_at": parse_date(headers["date"]),
        "body_text": body_text,
        "body_html": body_html,
        "attachments": extract_attachments(msg),
    }
