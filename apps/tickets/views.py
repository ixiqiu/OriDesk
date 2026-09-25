"""工单 Web 视图（开发文档 §7-6）。

约定：
- 所有视图都受 @login_required 保护，取工单一律走可见性查询（§5.6）。
- 写操作一律 POST + CSRF，成功后用 messages 反馈，再重定向（PRG 模式）。
- HTMX 请求返回局部模板，普通请求返回整页。
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_POST

from apps.accounts.models import Group
from apps.core.permissions import get_visible_ticket
from apps.core.utils import html_to_text
from apps.mailboxes import storage
from apps.mailboxes.sanitizer import sanitize_html
from apps.mailboxes.services import MailDeliveryError, identity_mailbox_for, send_reply
from apps.routing.services import reassign_ticket
from apps.tickets.forms import InboxFilterForm, NoteForm, ReassignForm, ReplyForm, StatusForm
from apps.tickets.models import Attachment, Ticket
from apps.tickets.selectors import visible_tickets
from apps.tickets.services import (
    add_note,
    claim_ticket,
    set_status,
    unclaim_ticket,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 25


# ------------------------------------------------------------------ 收件箱
@login_required
def inbox(request, scope: str = "all"):
    """统一收件箱 / 组收件箱（§7-6）。"""
    queryset = visible_tickets(request.user).select_related("group", "assignee", "mailbox")

    if scope == "mine":
        queryset = queryset.filter(assignee=request.user)
    elif scope == "unassigned":
        queryset = queryset.filter(assignee__isnull=True).exclude(status="closed")
    elif scope == "awaiting":
        queryset = queryset.filter(is_awaiting_reply=True).filter(
            Q(assignee__isnull=True) | Q(assignee=request.user)
        )

    form = InboxFilterForm(request.GET or None)
    filters = {"q": "", "group": "", "status": "", "awaiting": "", "assignee": ""}
    if form.is_valid():
        data = form.cleaned_data
        filters = {
            "q": data.get("q") or "",
            "group": data["group"].pk if data.get("group") else "",
            "status": data.get("status") or "",
            "awaiting": data.get("awaiting") or "",
            "assignee": data.get("assignee") or "",
        }
    else:
        # 手工解析 group / status 等，兼容直接点导航链接
        filters.update(
            {
                "q": request.GET.get("q", ""),
                "group": request.GET.get("group", ""),
                "status": request.GET.get("status", ""),
                "awaiting": request.GET.get("awaiting", ""),
                "assignee": request.GET.get("assignee", ""),
            }
        )

    if filters["q"]:
        needle = filters["q"]
        queryset = queryset.filter(
            Q(subject__icontains=needle)
            | Q(normalized_subject__icontains=needle)
            | Q(customer_email__icontains=needle)
            | Q(messages__body_text__icontains=needle)
        ).distinct()
    if filters["group"]:
        queryset = queryset.filter(group_id=filters["group"])
    if filters["status"]:
        queryset = queryset.filter(status=filters["status"])
    if filters["awaiting"] in ("0", "1"):
        queryset = queryset.filter(is_awaiting_reply=filters["awaiting"] == "1")
    if filters["assignee"]:
        if filters["assignee"] == "me":
            queryset = queryset.filter(assignee=request.user)
        elif filters["assignee"].isdigit():
            queryset = queryset.filter(assignee_id=int(filters["assignee"]))
        elif filters["assignee"] == "none":
            queryset = queryset.filter(assignee__isnull=True)

    queryset = queryset.order_by("-last_message_at", "-id")
    paginator = Paginator(queryset, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "page_obj": page_obj,
        "tickets": page_obj.object_list,
        "scope": scope,
        "filters": filters,
        "form": form,
        "groups": _selectable_groups(request.user),
    }
    template = "tickets/_inbox_table.html" if _is_htmx(request) else "tickets/inbox.html"
    return render(request, template, context)


def _selectable_groups(user):
    if user.sees_all_tickets:
        return Group.objects.all().order_by("name")
    return Group.objects.filter(user_groups__user=user).distinct().order_by("name")


# ------------------------------------------------------------------ 详情
@login_required
def detail(request, pk: int):
    ticket = get_visible_ticket(request.user, pk)
    context = _detail_context(request, ticket)
    return render(request, "tickets/detail.html", context)


def _detail_context(request, ticket: Ticket) -> dict:
    timeline = list(
        ticket.messages.select_related("actual_sender", "mailbox").prefetch_related("attachments")
    )
    return {
        "ticket": ticket,
        "timeline": timeline,
        "reply_form": ReplyForm(),
        "note_form": NoteForm(),
        "reassign_form": ReassignForm(ticket=ticket),
        "status_form": StatusForm(initial={"status": ticket.status}),
        "audit_logs": ticket.audit_logs.select_related("user", "group")[:30],
        "identity_email": _identity_email_safe(ticket),
        "can_reassign": True,
        "editor_id": "reply-editor",
    }


def _identity_email_safe(ticket: Ticket) -> str:
    try:
        return identity_mailbox_for(ticket).email
    except MailDeliveryError:
        return "（无可用对外邮箱）"


@login_required
def message_list(request, pk: int):
    """HTMX 局部刷新：仅返回时间线。"""
    ticket = get_visible_ticket(request.user, pk)
    timeline = list(
        ticket.messages.select_related("actual_sender", "mailbox").prefetch_related("attachments")
    )
    return render(request, "tickets/_timeline.html", {"ticket": ticket, "timeline": timeline})


# ------------------------------------------------------------------ 回复
@login_required
@require_POST
def reply(request, pk: int):
    ticket = get_visible_ticket(request.user, pk)
    form = ReplyForm(request.POST, request.FILES)
    if not form.is_valid():
        for error in form.errors.get("__all__", []):
            messages.error(request, error)
        if not form.errors.get("__all__"):
            messages.error(request, "回复内容不能为空。")
        return redirect("tickets:detail", pk=ticket.pk)

    body_html = sanitize_html(form.cleaned_data.get("body_html") or "")
    body_text = (form.cleaned_data.get("body_text") or "").strip()
    if not body_text and body_html:
        body_text = html_to_text(body_html)
    if not body_text:
        messages.error(request, "回复内容不能为空。")
        return redirect("tickets:detail", pk=ticket.pk)

    attachments = []
    for upload in form.cleaned_data.get("attachments") or []:
        attachments.append(
            {
                "filename": upload.name,
                "content": upload.read(),
                "mime": getattr(upload, "content_type", "") or "",
            }
        )

    try:
        send_reply(
            ticket,
            request.user,
            body_text,
            body_html=body_html,
            attachments=attachments,
            cc=form.cc_list() or None,
        )
    except MailDeliveryError as exc:
        logger.warning("工单 T#%s 回复发送失败：%s", ticket.pk, exc)
        messages.error(request, f"发信失败：{exc}")
    except Exception:  # noqa: BLE001
        logger.exception("工单 T#%s 回复出现未预期错误。", ticket.pk)
        messages.error(request, "发信失败，请查看系统日志后重试。")
    else:
        messages.success(request, "回复已发出，待回复标签已更新。")

    if _is_htmx(request):
        return message_list(request, pk)
    return redirect("tickets:detail", pk=ticket.pk)


# ------------------------------------------------------------------ 内部备注
@login_required
@require_POST
def create_note(request, pk: int):
    ticket = get_visible_ticket(request.user, pk)
    form = NoteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "备注内容不能为空。")
        return redirect("tickets:detail", pk=ticket.pk)

    add_note(ticket, request.user, html_to_text(sanitize_html(form.cleaned_data["body_text"])))
    messages.success(request, "内部备注已添加。")
    if _is_htmx(request):
        return message_list(request, pk)
    return redirect("tickets:detail", pk=ticket.pk)


# ------------------------------------------------------------------ 认领 / 改派 / 状态
@login_required
@require_POST
def claim(request, pk: int):
    ticket = get_visible_ticket(request.user, pk)
    claim_ticket(ticket, request.user)
    messages.success(request, f"已认领工单 [T#{ticket.pk}]。")
    return _back_to_ticket(request, ticket)


@login_required
@require_POST
def unclaim(request, pk: int):
    ticket = get_visible_ticket(request.user, pk)
    try:
        unclaim_ticket(ticket, request.user)
    except PermissionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "已取消认领。")
    return _back_to_ticket(request, ticket)


@login_required
@require_POST
def reassign(request, pk: int):
    ticket = get_visible_ticket(request.user, pk)
    form = ReassignForm(request.POST, ticket=ticket)
    if not form.is_valid():
        messages.error(request, "请选择有效的目标用户组。")
        return _back_to_ticket(request, ticket)
    try:
        reassign_ticket(
            ticket,
            form.cleaned_data["group"],
            request.user,
            reason=form.cleaned_data.get("reason", ""),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"已改派到「{ticket.group.name}」，原组不再可见。")
    return _back_to_ticket(request, ticket)


@login_required
@require_POST
def update_status(request, pk: int):
    ticket = get_visible_ticket(request.user, pk)
    form = StatusForm(request.POST)
    if not form.is_valid():
        messages.error(request, "工单状态无效。")
    else:
        set_status(ticket, form.cleaned_data["status"], user=request.user)
        messages.success(request, f"工单状态已更新为「{ticket.get_status_display()}」。")
    return _back_to_ticket(request, ticket)


def _back_to_ticket(request, ticket: Ticket):
    if _is_htmx(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("tickets:detail", args=[ticket.pk])})
    return redirect("tickets:detail", pk=ticket.pk)


# ------------------------------------------------------------------ 附件
@login_required
def attachment_download(request, pk: int):
    attachment = _visible_attachment(request.user, pk)
    return _serve_attachment(attachment, as_attachment=True)


@login_required
def attachment_preview(request, pk: int):
    attachment = _visible_attachment(request.user, pk)
    if attachment.is_dangerous:
        raise PermissionDenied("该附件类型不允许在线预览，请下载后自行确认安全性。")
    return _serve_attachment(attachment, as_attachment=False)


def _serve_attachment(attachment: Attachment, *, as_attachment: bool):
    """在完成可见性校验后把附件发给客户端。

    生产环境推荐配置 `ATTACHMENT_X_ACCEL_PREFIX`（如 `/media/attachments/`），
    由 Nginx 的 `internal` location 负责传输（X-Accel-Redirect），
    这样 `/media/` 目录不必对外暴露：未通过 Django 鉴权的直链一律 404。
    未配置时由 Django 直接流式返回（本地开发/单机部署可用）。
    """
    content_type = attachment.mime or "application/octet-stream"
    disposition = content_disposition_header(as_attachment, attachment.filename)
    prefix = getattr(settings, "ATTACHMENT_X_ACCEL_PREFIX", "") or ""

    if prefix:
        response = HttpResponse(content_type=content_type)
        response["Content-Disposition"] = disposition
        # 头部值必须是 ASCII：附件名可能含中文，这里做百分号编码，
        # 否则 Django 会按 RFC2047 编码整个头部，Nginx 无法解析。
        response["X-Accel-Redirect"] = f"{prefix.rstrip('/')}/{quote(attachment.path)}"
        return response

    try:
        handle = storage.absolute_path(attachment.path).open("rb")
    except (OSError, ValueError) as exc:
        raise Http404("附件文件已不存在。") from exc
    response = FileResponse(handle, as_attachment=as_attachment, filename=attachment.filename)
    response["Content-Type"] = content_type
    return response


def _visible_attachment(user, pk: int) -> Attachment:
    attachment = (
        Attachment.objects.select_related("message", "message__ticket")
        .filter(pk=pk)
        .first()
    )
    if attachment is None:
        raise Http404("附件不存在。")
    if visible_tickets(user).filter(pk=attachment.message.ticket_id).exists():
        return attachment
    raise Http404("附件不存在。")


def _is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"
