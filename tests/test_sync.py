"""IMAP 增量同步验收用例（开发文档 §6.4）。

用假 IMAP 服务器替代真实网络：只验证同步位点语义，不依赖外部服务。
重点验证 DEV-5 的保守策略：失败邮件不推进 last_uid，下一轮会重试。
"""

from __future__ import annotations

from django.utils import timezone

from apps.mailboxes.sync import MailSyncError, sync_all_mailboxes, sync_mailbox
from apps.tickets.models import Ticket
from tests.conftest import make_mailbox
from tests.helpers import build_raw


class FakeIMAPClient:
    """imapclient.IMAPClient 的最小替身。"""

    def __init__(self, host, port=None, ssl=True, timeout=None):
        self.host = host
        self.port = port
        self.ssl = ssl
        self.uidvalidity = 100
        self.uids: list[int] = []
        self.payloads: dict[int, bytes] = {}
        self.searched = None
        self.credentials = None
        self.selected_folder = None
        self.closed = False

    # 上下文管理
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    # IMAP 命令
    def login(self, username, secret):
        self.credentials = (username, secret)

    def select_folder(self, folder):
        self.selected_folder = folder

    def folder_status(self, folder, what=None):
        return {b"UIDVALIDITY": self.uidvalidity, b"MESSAGES": len(self.uids)}

    def search(self, criteria):
        self.searched = criteria
        return list(self.uids)

    def fetch(self, uids, parts):
        return {uid: {b"RFC822": self.payloads[uid]} for uid in uids}


def install_fake(monkeypatch, client: FakeIMAPClient):
    monkeypatch.setattr("imapclient.IMAPClient", lambda *args, **kwargs: client)
    return client


def make_payload(subject: str, sender: str = "customer@customer-domain.com", message_id: str | None = None):
    return build_raw(sender=sender, subject=subject, message_id=message_id)


def test_sync_processes_new_mail_and_advances_last_uid(
    authenticated_rule, unified_mailbox, monkeypatch, outbox
):
    client = FakeIMAPClient("imap.test")
    client.uidvalidity = 100
    client.uids = [1, 2]
    client.payloads = {1: make_payload("无法登录后台", message_id="<s1@x>"), 2: make_payload("无法登录后台", message_id="<s2@x>")}
    install_fake(monkeypatch, client)

    stats = sync_mailbox(unified_mailbox)

    assert stats["fetched"] == 2
    assert stats["processed"] == 2
    assert stats["failed"] == 0
    unified_mailbox.refresh_from_db()
    assert unified_mailbox.last_uid == 2
    assert unified_mailbox.uidvalidity == 100
    assert Ticket.objects.count() == 2
    # 登录用的是解密后的凭据，而不是密文
    assert client.credentials == (unified_mailbox.email, "demo-auth-code")
    assert client.selected_folder == "INBOX"


def test_sync_is_incremental(authenticated_rule, unified_mailbox, monkeypatch, outbox):
    unified_mailbox.last_uid = 5
    unified_mailbox.uidvalidity = 100
    unified_mailbox.save(update_fields=["last_uid", "uidvalidity"])

    client = FakeIMAPClient("imap.test")
    client.uidvalidity = 100
    client.uids = [5, 6]
    client.payloads = {6: make_payload("无法登录后台", message_id="<s6@x>")}
    install_fake(monkeypatch, client)

    sync_mailbox(unified_mailbox)

    # 只请求 6:*，并且不会重复处理 uid=5
    assert client.searched == ["UID", "6:*"]
    unified_mailbox.refresh_from_db()
    assert unified_mailbox.last_uid == 6
    assert Ticket.objects.count() == 1


def test_sync_resets_position_when_uidvalidity_changes(
    authenticated_rule, unified_mailbox, monkeypatch, outbox
):
    unified_mailbox.last_uid = 42
    unified_mailbox.uidvalidity = 100
    unified_mailbox.save(update_fields=["last_uid", "uidvalidity"])

    client = FakeIMAPClient("imap.test")
    client.uidvalidity = 777  # 服务器端 UIDVALIDITY 变了
    client.uids = [1]
    client.payloads = {1: make_payload("无法登录后台", message_id="<r1@x>")}
    install_fake(monkeypatch, client)

    sync_mailbox(unified_mailbox)

    assert client.searched == ["UID", "1:*"]
    unified_mailbox.refresh_from_db()
    assert unified_mailbox.uidvalidity == 777
    assert unified_mailbox.last_uid == 1
    assert Ticket.objects.count() == 1


def test_sync_does_not_advance_past_failed_message(
    authenticated_rule, unified_mailbox, monkeypatch, outbox
):
    """第 2 封失败时，位点只推进到第 1 封，下一轮会重试第 2 封（DEV-5）。"""
    client = FakeIMAPClient("imap.test")
    client.uids = [1, 2, 3]
    client.payloads = {
        1: make_payload("无法登录后台", message_id="<f1@x>"),
        2: make_payload("无法登录后台", message_id="<f2@x>"),
        3: make_payload("无法登录后台", message_id="<f3@x>"),
    }
    install_fake(monkeypatch, client)

    calls = {"n": 0}
    real_process = __import__("apps.mailboxes.pipeline", fromlist=["process_inbound"]).process_inbound

    def flaky(mailbox, uid, raw, now=None):
        calls["n"] += 1
        if uid == 2:
            raise RuntimeError("模拟解析失败")
        return real_process(mailbox, uid, raw, now=now)

    monkeypatch.setattr("apps.mailboxes.pipeline.process_inbound", flaky)
    monkeypatch.setattr("apps.mailboxes.sync.process_inbound", flaky)

    stats = sync_mailbox(unified_mailbox)

    assert stats["processed"] == 1
    assert stats["failed"] == 1
    unified_mailbox.refresh_from_db()
    assert unified_mailbox.last_uid == 1
    assert Ticket.objects.count() == 1


def test_sync_skips_loop_mail_but_advances(
    authenticated_rule, unified_mailbox, monkeypatch, outbox
):
    client = FakeIMAPClient("imap.test")
    client.uids = [1]
    client.payloads = {
        1: build_raw(
            sender="customer@customer-domain.com",
            subject="自动回复",
            headers={"Auto-Submitted": "auto-generated"},
            message_id="<loop@x>",
        )
    }
    install_fake(monkeypatch, client)

    stats = sync_mailbox(unified_mailbox)

    assert stats["processed"] == 0
    assert stats["skipped"] == 1
    unified_mailbox.refresh_from_db()
    assert unified_mailbox.last_uid == 1
    assert Ticket.objects.count() == 0


def test_sync_all_isolates_mailbox_failures(unified_mailbox, monkeypatch, outbox):
    broken = make_mailbox(email="broken@example.com", name="故障邮箱")

    def fake_sync(mailbox, limit=None, now=None):
        if mailbox.pk == broken.pk:
            raise MailSyncError("凭据不可用")
        return {"mailbox": mailbox.email, "processed": 1}

    monkeypatch.setattr("apps.mailboxes.sync.sync_mailbox", fake_sync)
    results = sync_all_mailboxes()

    assert len(results) == 2
    assert any(item.get("error") for item in results)
    assert any(item.get("processed") == 1 for item in results)


def test_sync_returns_early_when_nothing_new(authenticated_rule, unified_mailbox, monkeypatch, outbox):
    client = FakeIMAPClient("imap.test")
    client.uids = []
    install_fake(monkeypatch, client)

    stats = sync_mailbox(unified_mailbox)
    assert stats["fetched"] == 0
    unified_mailbox.refresh_from_db()
    assert unified_mailbox.last_uid == 0
    assert timezone.now() is not None
