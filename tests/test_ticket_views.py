"""工单 Web 界面验收用例（登录保护 / 可见性 / 回复 / 认领 / 改派 / 附件）。"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.mailboxes import storage
from apps.tickets.models import Attachment, Message, Ticket
from tests.conftest import make_message, make_ticket

CUSTOMER = "customer@customer-domain.com"
PASSWORD = "DemoPass!2345"


@pytest.fixture
def media_root(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


@pytest.fixture
def ticket(unified_mailbox, tech_group):
    return make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)


# ------------------------------------------------------------------ 访问控制
def test_inbox_requires_login(client, ticket):
    response = client.get(reverse("tickets:inbox"))
    assert response.status_code == 302
    assert reverse("accounts:login") in response["Location"]


def test_healthz_is_public(client, db):
    response = client.get(reverse("healthz"))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_inbox_shows_only_visible_tickets(
    client, tech_user, unified_mailbox, tech_group, finance_group
):
    mine = make_ticket(mailbox=unified_mailbox, group=tech_group, subject="技术问题")
    other = make_ticket(mailbox=unified_mailbox, group=finance_group, subject="发票问题")
    client.force_login(tech_user)

    response = client.get(reverse("tickets:inbox"))
    content = response.content.decode()
    assert response.status_code == 200
    assert f"T#{mine.pk}" in content
    assert f"T#{other.pk}" not in content


def test_detail_of_other_group_returns_404(
    client, tech_user, unified_mailbox, finance_group
):
    hidden = make_ticket(mailbox=unified_mailbox, group=finance_group, subject="发票问题")
    client.force_login(tech_user)
    response = client.get(reverse("tickets:detail", args=[hidden.pk]))
    assert response.status_code == 404


def test_detail_renders_timeline(client, tech_user, ticket, unified_mailbox):
    make_message(ticket=ticket, mailbox=unified_mailbox, body_text="客户来信内容")
    client.force_login(tech_user)
    response = client.get(reverse("tickets:detail", args=[ticket.pk]))
    assert response.status_code == 200
    assert "客户来信内容" in response.content.decode()


# ------------------------------------------------------------------ 回复
@pytest.mark.django_db
def test_reply_sends_and_clears_awaiting(client, tech_user, tech_mailbox, ticket, outbox):
    ticket.is_awaiting_reply = True
    ticket.save(update_fields=["is_awaiting_reply"])
    client.force_login(tech_user)

    response = client.post(
        reverse("tickets:reply", args=[ticket.pk]),
        {"body_text": "已为你处理完成。", "body_html": "", "cc": ""},
        follow=True,
    )

    assert response.status_code == 200
    ticket.refresh_from_db()
    assert ticket.is_awaiting_reply is False
    assert Message.objects.filter(ticket=ticket, direction="out", type="message").count() == 1
    assert len(outbox) == 1
    assert outbox[0]["msg"]["From"] == "tech@example.com"


def test_reply_rejects_empty_body(client, tech_user, tech_mailbox, ticket, outbox):
    client.force_login(tech_user)
    response = client.post(
        reverse("tickets:reply", args=[ticket.pk]),
        {"body_text": "", "body_html": "", "cc": ""},
        follow=True,
    )
    assert response.status_code == 200
    assert Message.objects.filter(ticket=ticket, direction="out").count() == 0
    assert outbox == []


def test_reply_sanitizes_html(client, tech_user, tech_mailbox, ticket, outbox):
    client.force_login(tech_user)
    client.post(
        reverse("tickets:reply", args=[ticket.pk]),
        {
            "body_text": "",
            "body_html": '<p>正常内容</p><script>alert(1)</script>',
            "cc": "",
        },
        follow=True,
    )
    message = Message.objects.get(ticket=ticket, direction="out", type="message")
    assert "<script" not in message.body_html.lower()
    assert "正常内容" in message.body_html
    assert "正常内容" in message.body_text


# ------------------------------------------------------------------ 备注 / 认领 / 改派 / 状态
def test_note_is_internal_and_keeps_awaiting(client, tech_user, ticket):
    ticket.is_awaiting_reply = True
    ticket.save(update_fields=["is_awaiting_reply"])
    client.force_login(tech_user)

    client.post(reverse("tickets:note", args=[ticket.pk]), {"body_text": "内部沟通：客户已升级"}, follow=True)

    note = Message.objects.get(ticket=ticket, type="note")
    assert note.actual_sender == tech_user
    ticket.refresh_from_db()
    assert ticket.is_awaiting_reply is True


def test_claim_and_unclaim(client, tech_user, ticket):
    client.force_login(tech_user)
    client.post(reverse("tickets:claim", args=[ticket.pk]), follow=True)
    ticket.refresh_from_db()
    assert ticket.assignee == tech_user

    client.post(reverse("tickets:unclaim", args=[ticket.pk]), follow=True)
    ticket.refresh_from_db()
    assert ticket.assignee is None
    assert AuditLog.objects.filter(ticket=ticket, action="claim").exists()
    assert AuditLog.objects.filter(ticket=ticket, action="unclaim").exists()


def test_reassign_moves_group_and_writes_audit(
    client, tech_user, ticket, finance_group
):
    client.force_login(tech_user)
    client.post(
        reverse("tickets:reassign", args=[ticket.pk]),
        {"group": finance_group.pk, "reason": "财务范围"},
        follow=True,
    )
    ticket.refresh_from_db()
    assert ticket.group == finance_group
    assert AuditLog.objects.filter(ticket=ticket, action="forward").exists()


def test_update_status(client, tech_user, ticket):
    client.force_login(tech_user)
    client.post(reverse("tickets:set_status", args=[ticket.pk]), {"status": "closed"}, follow=True)
    ticket.refresh_from_db()
    assert ticket.status == "closed"


def test_inbox_filter_by_group(client, superadmin, unified_mailbox, tech_group, finance_group):
    tech_ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, subject="技术问题")
    finance_ticket = make_ticket(mailbox=unified_mailbox, group=finance_group, subject="发票问题")
    client.force_login(superadmin)

    response = client.get(reverse("tickets:inbox"), {"group": finance_group.pk})
    content = response.content.decode()
    assert f"T#{finance_ticket.pk}" in content
    assert f"T#{tech_ticket.pk}" not in content


def test_inbox_search_matches_body(client, tech_user, ticket, unified_mailbox):
    make_message(ticket=ticket, mailbox=unified_mailbox, body_text="关键字：蓝屏死机")
    client.force_login(tech_user)
    response = client.get(reverse("tickets:inbox"), {"q": "蓝屏"})
    assert f"T#{ticket.pk}" in response.content.decode()


# ------------------------------------------------------------------ 附件
def _make_attachment(media_root, ticket, unified_mailbox, filename="报告.pdf", dangerous=False):
    message = make_message(ticket=ticket, mailbox=unified_mailbox)
    if dangerous:
        filename = "tool.exe"
    rel = storage.attachment_rel_path(ticket.pk, "msg-1", filename)
    storage.save_file(rel, b"file-content")
    return Attachment.objects.create(
        message=message, filename=filename, mime="application/octet-stream", size=12, path=rel
    )


def test_attachment_download_allows_visible_user(client, tech_user, ticket, unified_mailbox, media_root):
    attachment = _make_attachment(media_root, ticket, unified_mailbox)
    client.force_login(tech_user)
    response = client.get(reverse("tickets:attachment_download", args=[attachment.pk]))
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"file-content"


def test_attachment_download_hidden_from_other_group(
    client, tech_user, unified_mailbox, finance_group, media_root
):
    hidden_ticket = make_ticket(mailbox=unified_mailbox, group=finance_group)
    attachment = _make_attachment(media_root, hidden_ticket, unified_mailbox)
    client.force_login(tech_user)
    response = client.get(reverse("tickets:attachment_download", args=[attachment.pk]))
    assert response.status_code == 404


def test_attachment_preview_rejects_dangerous(
    client, tech_user, ticket, unified_mailbox, media_root
):
    attachment = _make_attachment(media_root, ticket, unified_mailbox, dangerous=True)
    client.force_login(tech_user)
    response = client.get(reverse("tickets:attachment_preview", args=[attachment.pk]))
    assert response.status_code == 403


def test_attachment_preview_allows_safe(
    client, tech_user, ticket, unified_mailbox, media_root
):
    attachment = _make_attachment(media_root, ticket, unified_mailbox)
    client.force_login(tech_user)
    response = client.get(reverse("tickets:attachment_preview", args=[attachment.pk]))
    assert response.status_code == 200


@pytest.mark.django_db
def test_attachment_download_uses_x_accel_when_configured(
    client, tech_user, ticket, unified_mailbox, media_root, settings
):
    """生产推荐：由 Nginx internal location 传输，/media/ 不必对外暴露。"""
    attachment = _make_attachment(media_root, ticket, unified_mailbox)
    settings.ATTACHMENT_X_ACCEL_PREFIX = "/media/attachments/"
    client.force_login(tech_user)

    response = client.get(reverse("tickets:attachment_download", args=[attachment.pk]))
    assert response.status_code == 200
    from urllib.parse import quote

    assert response["X-Accel-Redirect"] == f"/media/attachments/{quote(attachment.path)}"
    assert response["X-Accel-Redirect"].isascii()
    assert "attachment" in response["Content-Disposition"]


@pytest.mark.django_db
def test_attachment_download_x_accel_still_enforces_visibility(
    client, tech_user, unified_mailbox, finance_group, media_root, settings
):
    hidden_ticket = make_ticket(mailbox=unified_mailbox, group=finance_group)
    attachment = _make_attachment(media_root, hidden_ticket, unified_mailbox)
    settings.ATTACHMENT_X_ACCEL_PREFIX = "/media/attachments/"
    client.force_login(tech_user)

    response = client.get(reverse("tickets:attachment_download", args=[attachment.pk]))
    assert response.status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"group": "abc"},
        {"group": "999999"},
        {"status": "not-a-status"},
        {"awaiting": "maybe"},
        {"assignee": "???"},
        {"page": "abc"},
        {"page": "9999"},
        {"q": "'; DROP TABLE tickets; --"},
        {"q": "%00"},
    ],
)
def test_inbox_survives_bad_query_params(client, tech_user, ticket, params):
    """坏查询参数不能让列表页 500，也不能造成注入。"""
    client.force_login(tech_user)
    response = client.get(reverse("tickets:inbox"), params)
    assert response.status_code == 200
    assert Ticket.objects.filter(pk=ticket.pk).exists()
