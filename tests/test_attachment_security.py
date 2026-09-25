"""附件安全回归（存储型 XSS / 扩展名绕过 / Nginx 配置不变量）。

对应独立验证报告中的 [严重] 与 [中] 两项问题，以及 X-Accel 配置漂移风险。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, unquote

import pytest
from django.conf import settings
from django.urls import reverse

from apps.mailboxes import storage
from apps.tickets.models import DANGEROUS_EXTENSIONS, Attachment
from tests.conftest import make_message, make_ticket

CUSTOMER = "customer@customer-domain.com"


@pytest.fixture
def media_root(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def make_attachment(media_root, ticket, mailbox, *, filename, mime, content=b"payload"):
    message = make_message(ticket=ticket, mailbox=mailbox)
    rel = storage.attachment_rel_path(ticket.pk, "msg-1", filename)
    storage.save_file(rel, content)
    return Attachment.objects.create(
        message=message, filename=filename, mime=mime, size=len(content), path=rel
    )


# --------------------------------------------------------------- 扩展名规范化
@pytest.mark.parametrize(
    "filename,expected_dangerous",
    [
        ("tool.exe", True),
        ("TOOL.EXE", True),
        ("tool.exe.", True),  # Windows/浏览器会剥掉结尾的点
        ("tool.exe ", True),  # 同上，结尾空格
        ("tool.exe.  ", True),
        ("tool.exe\u00a0", True),
        ("invoice.pdf", False),
        ("tool.exe.pdf", False),  # 真实类型是 pdf
        ("exe", False),
        ("", False),
        ("../../tool.exe", True),
        ("C:\\temp\\tool.bat", True),
    ],
)
def test_dangerous_extension_normalization(db, filename, expected_dangerous):
    attachment = Attachment(filename=filename, mime="", size=0, path="x", message_id=None)
    assert attachment.is_dangerous is expected_dangerous


@pytest.mark.parametrize("ext", sorted(DANGEROUS_EXTENSIONS))
def test_all_documented_dangerous_extensions_are_not_previewable(db, ext):
    attachment = Attachment(filename=f"file{ext}", mime="", size=0, path="x")
    assert attachment.is_dangerous is True
    assert attachment.is_previewable is False


# --------------------------------------------------------------- 预览类型白名单
ACTIVE_CONTENT_CASES = [
    ("report.html", "text/html"),
    ("report.htm", "text/html"),
    ("image.svg", "image/svg+xml"),
    ("report.xhtml", "application/xhtml+xml"),
    ("data.xml", "text/xml"),
    ("script.js", "application/javascript"),
    ("page.mhtml", "multipart/related"),
    ("unknown.bin", "application/octet-stream"),
    ("tool.exe", "application/octet-stream"),
    ("report.html", ""),  # 没有 MIME 头也不能靠扩展名混进预览
]


@pytest.mark.django_db
@pytest.mark.parametrize("filename,mime", ACTIVE_CONTENT_CASES)
def test_active_content_cannot_be_previewed(
    client, tech_user, unified_mailbox, tech_group, media_root, filename, mime
):
    """活动内容（HTML/SVG/XHTML/脚本/未知类型）必须只能下载，不能内联预览。"""
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    attachment = make_attachment(
        media_root,
        ticket,
        unified_mailbox,
        filename=filename,
        mime=mime,
        content=b"<html><body><script>alert(document.cookie)</script></body></html>",
    )
    client.force_login(tech_user)

    preview = client.get(reverse("tickets:attachment_preview", args=[attachment.pk]))
    assert preview.status_code == 403, f"{filename}/{mime} 仍可预览"

    # 下载仍可用，但必须带 nosniff，并以 attachment 方式下发
    download = client.get(reverse("tickets:attachment_download", args=[attachment.pk]))
    assert download.status_code == 200
    assert download["X-Content-Type-Options"] == "nosniff"
    assert "attachment" in download["Content-Disposition"]


SAFE_PREVIEW_CASES = [
    ("photo.png", "image/png", "image/png"),
    ("photo.jpg", "image/jpeg", "image/jpeg"),
    ("notes.txt", "text/plain", "text/plain"),
    ("report.pdf", "application/pdf", "application/pdf"),
    ("photo.png", "application/octet-stream", "image/png"),  # 靠扩展名兜底
    ("photo.png", "text/html", "image/png"),  # 无视发件人伪造的 text/html
]


@pytest.mark.django_db
@pytest.mark.parametrize("filename,mime,expected_type", SAFE_PREVIEW_CASES)
def test_preview_uses_whitelisted_content_type(
    client, tech_user, unified_mailbox, tech_group, media_root, filename, mime, expected_type
):
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    attachment = make_attachment(
        media_root, ticket, unified_mailbox, filename=filename, mime=mime, content=b"binary"
    )
    client.force_login(tech_user)

    response = client.get(reverse("tickets:attachment_preview", args=[attachment.pk]))
    assert response.status_code == 200
    assert response["Content-Type"] == expected_type
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in response["Content-Security-Policy"]
    assert response["Content-Security-Policy"].startswith("default-src 'none'")


def test_attachment_properties_for_uploaded_html():
    attachment = Attachment(filename="report.html", mime="text/html", size=10, path="x")
    assert attachment.is_previewable is False
    assert attachment.effective_mime == ""  # 不在白名单，也没有安全扩展名兜底


# --------------------------------------------------------------- X-Accel / Nginx 契约
@pytest.mark.django_db
def test_x_accel_uri_maps_to_real_file_through_nginx_alias(
    client, tech_user, unified_mailbox, tech_group, media_root, settings
):
    """端到端校验路径映射：按 Nginx alias 规则换算后必须等于磁盘真实文件。

    Nginx 配置（deploy/nginx/ticket.example.com.conf）：
        location ^~ /media/attachments/ { internal; alias <MEDIA_ROOT>/; }
    即 URI 中 location 前缀之后的部分会拼到 alias 目录后面。
    """
    settings.ATTACHMENT_X_ACCEL_PREFIX = "/media/attachments/"
    ticket = make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)
    attachment = make_attachment(
        media_root, ticket, unified_mailbox, filename="季度报价单 2026.xlsx", mime="application/vnd.ms-excel"
    )
    client.force_login(tech_user)

    response = client.get(reverse("tickets:attachment_download", args=[attachment.pk]))
    uri = response["X-Accel-Redirect"]
    assert uri.isascii()

    location_prefix = "/media/attachments/"
    assert uri.startswith(location_prefix)
    remainder = unquote(uri[len(location_prefix):])
    mapped = Path(settings.MEDIA_ROOT) / remainder  # alias 指向 MEDIA_ROOT
    assert mapped == Path(settings.MEDIA_ROOT) / attachment.path
    assert mapped.exists()

    # 前缀语义：X-Accel URI = 前缀 + 数据库相对路径
    assert uri == f"{location_prefix}{quote(attachment.path)}"


def test_nginx_config_keeps_attachments_internal():
    """配置漂移守卫：有人把附件 location 改回直出时，本用例必须失败。"""
    conf = Path(__file__).resolve().parent.parent / "deploy" / "nginx" / "ticket.example.com.conf"
    text = conf.read_text(encoding="utf-8")
    block = re.search(r"location\s+\^~\s+/media/attachments/\s*\{(.*?)\n\s*\}", text, re.S)
    assert block, "未找到 /media/attachments/ location"
    body = block.group(1)
    assert re.search(r"^\s*internal;", body, re.M), "附件 location 必须为 internal（禁止匿名直链下载）"
    assert re.search(r"alias\s+/srv/ticket-system/media/;", body), (
        "alias 必须指向媒体根目录：X-Accel URI 已含一层 attachments/，"
        "若 alias 指到 media/attachments/ 会多一层导致 404"
    )


def test_settings_expose_x_accel_prefix():
    assert hasattr(settings, "ATTACHMENT_X_ACCEL_PREFIX")
