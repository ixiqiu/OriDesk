"""附件存储（开发文档 §6.3）。

存储路径：media/attachments/{ticket_id}/{message_key}/{filename}
- 文件名与目录名一律做安全化处理，杜绝路径穿越。
- 大小上限由 Setting.max_attachment_size_mb 控制。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._@-]+")
_MAX_COMPONENT = 120


def safe_component(value: str, fallback: str = "unknown") -> str:
    """把任意字符串变成安全的路径片段（防路径穿越）。"""
    raw = (value or "").strip()
    raw = raw.replace("\\", "/").split("/")[-1]
    raw = unicodedata.normalize("NFKC", raw)
    raw = _UNSAFE_CHARS.sub("_", raw)
    raw = raw.strip("._") or fallback
    return raw[:_MAX_COMPONENT]


def attachment_rel_dir(ticket_id: int | str, message_key: str) -> str:
    return f"attachments/{safe_component(str(ticket_id), '0')}/{safe_component(str(message_key), 'msg')}"


def attachment_rel_path(ticket_id: int | str, message_key: str, filename: str) -> str:
    return f"{attachment_rel_dir(ticket_id, message_key)}/{safe_component(filename, 'file')}"


def absolute_path(rel_path: str) -> Path:
    root = Path(settings.MEDIA_ROOT).resolve()
    target = (root / rel_path).resolve()
    if root not in target.parents and target != root:
        raise ValueError("附件路径越界，已拒绝。")
    return target


def max_attachment_bytes() -> int:
    from apps.audit.models import Setting

    mb = Setting.get_int("max_attachment_size_mb", 25)
    return max(1, mb) * 1024 * 1024


def save_file(rel_path: str, content: bytes) -> str:
    """写入附件文件，返回相对路径（存数据库用）。"""
    path = absolute_path(rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return rel_path


def read_file(rel_path: str) -> bytes:
    return absolute_path(rel_path).read_bytes()


def delete_file(rel_path: str) -> None:
    try:
        absolute_path(rel_path).unlink(missing_ok=True)
    except (OSError, ValueError):  # pragma: no cover - 清理失败不影响业务
        logger.warning("删除附件文件失败：%s", rel_path)


def store_inbound_attachment(
    *,
    ticket_id: int,
    message_key: str,
    filename: str,
    content: bytes,
    mime: str = "",
    limit_bytes: int | None = None,
) -> dict | None:
    """把入站附件落盘，返回 Attachment 建表所需字段；超限返回 None。"""
    limit = limit_bytes if limit_bytes is not None else max_attachment_bytes()
    size = len(content or b"")
    if size > limit:
        logger.warning(
            "附件 %s 大小 %s 字节超过上限 %s 字节，已跳过保存。", filename, size, limit
        )
        return None
    rel_path = attachment_rel_path(ticket_id, message_key, filename)
    save_file(rel_path, content)
    return {"filename": filename, "mime": mime, "size": size, "path": rel_path}


def store_outbound_attachment(*, ticket_id: int, message_key: str, filename: str, content: bytes, mime: str = "") -> dict:
    """外发附件落盘（与入站同一目录规范）。"""
    rel_path = attachment_rel_path(ticket_id, message_key, filename)
    save_file(rel_path, content)
    return {"filename": filename, "mime": mime, "size": len(content or b""), "path": rel_path}
