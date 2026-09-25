"""IMAP 增量同步（开发文档 §6.4）。

每个邮箱独立调用，由 APScheduler 定时触发（apps.core.management.commands.runapscheduler）。

与文档伪代码的差异（更保守，避免丢信）：
- 只把 `last_uid` 推进到**连续成功处理**的最后一封；中途失败则停止推进，
  下一轮会重试该封邮件（文档伪代码无条件 `last_uid = max(uids)` 会跳过失败邮件）。
- 增加幂等保护：同一 Message-ID 已入库则跳过（见 pipeline.already_seen）。
"""

from __future__ import annotations

import logging

from django.db import DatabaseError
from django.utils import timezone

from apps.accounts.models import Mailbox
from apps.mailboxes.pipeline import process_inbound

logger = logging.getLogger(__name__)


class MailSyncError(Exception):
    """IMAP 同步失败。"""


def _status_value(status, key: str):
    """兼容 folder_status 返回 bytes 或 str 键（imapclient 2.x/4.x 差异）。"""
    for candidate in (key.encode("ascii"), key):
        try:
            if candidate in status:
                return status[candidate]
        except TypeError:  # pragma: no cover - 非映射类型
            return None
    return None


def sync_mailbox(mailbox: Mailbox, *, limit: int | None = None, now=None) -> dict:
    """增量拉取指定邮箱的新邮件，返回统计信息。"""
    from imapclient import IMAPClient

    try:
        secret = mailbox.get_secret()
    except Exception as exc:  # noqa: BLE001 - 凭据不可用时给出明确错误
        raise MailSyncError(f"邮箱 {mailbox.email} 凭据不可用：{exc}") from exc

    stats = {"mailbox": mailbox.email, "fetched": 0, "processed": 0, "skipped": 0, "failed": 0}

    with IMAPClient(
        mailbox.imap_host, port=mailbox.imap_port, ssl=mailbox.imap_ssl, timeout=60
    ) as client:
        client.login(mailbox.username, secret)
        client.select_folder("INBOX")

        status = client.folder_status("INBOX")
        uidvalidity = int(_status_value(status, "UIDVALIDITY") or 0)
        last_uid = mailbox.last_uid
        if uidvalidity and uidvalidity != mailbox.uidvalidity:
            logger.warning(
                "邮箱 %s 的 UIDVALIDITY 变化（%s → %s），重置同步位点。",
                mailbox.email,
                mailbox.uidvalidity,
                uidvalidity,
            )
            last_uid = 0
            mailbox.uidvalidity = uidvalidity
            mailbox.last_uid = 0
            mailbox.save(update_fields=["uidvalidity", "last_uid"])

        uids = sorted(uid for uid in client.search(["UID", f"{last_uid + 1}:*"]) if uid > last_uid)
        if limit:
            uids = uids[:limit]
        if not uids:
            return stats

        stats["fetched"] = len(uids)
        target = timezone.now() if now is None else now
        highest_ok = last_uid

        for uid, data in client.fetch(uids, ["RFC822"]).items():
            raw = data.get(b"RFC822")
            if not raw:
                highest_ok = max(highest_ok, uid)
                continue
            try:
                result = process_inbound(mailbox, uid, raw, now=target)
            except Exception:  # noqa: BLE001 - 单封失败不阻塞邮箱，但不再推进位点
                logger.exception("邮箱 %s UID=%s 处理失败，将在下一轮重试。", mailbox.email, uid)
                stats["failed"] += 1
                break
            if result.processed:
                stats["processed"] += 1
            else:
                stats["skipped"] += 1
            highest_ok = max(highest_ok, uid)

        if highest_ok != mailbox.last_uid:
            try:
                mailbox.last_uid = highest_ok
                mailbox.save(update_fields=["last_uid"])
            except DatabaseError:  # pragma: no cover
                logger.exception("更新邮箱 %s 的 last_uid 失败。", mailbox.email)

    logger.info(
        "邮箱 %s 同步完成：拉取 %s，入库 %s，跳过 %s，失败 %s",
        stats["mailbox"],
        stats["fetched"],
        stats["processed"],
        stats["skipped"],
        stats["failed"],
    )
    return stats


def sync_all_mailboxes(*, limit: int | None = None) -> list[dict]:
    """同步所有启用邮箱，单个邮箱失败不影响其他邮箱。"""
    results: list[dict] = []
    for mailbox in Mailbox.objects.filter(is_active=True).order_by("id"):
        try:
            results.append(sync_mailbox(mailbox, limit=limit))
        except Exception as exc:  # noqa: BLE001
            logger.exception("邮箱 %s 同步失败：%s", mailbox.email, exc)
            results.append({"mailbox": mailbox.email, "error": str(exc), "processed": 0})
    return results
