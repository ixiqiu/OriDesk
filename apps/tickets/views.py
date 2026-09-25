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
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
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
from apps.tickets.forms import (
    InboxFilterForm,
    NoteForm,
    ReassignForm,
    ReplyForm,
    StatusForm,
    TicketTagForm,
)
from apps.tickets.models import MAX_TAGS_PER_TICKET, Attachment, Tag, Ticket
from apps.tickets.selectors import visible_tickets
from apps.tickets.services import (
    add_note,
    add_tag,
    claim_ticket,
    remove_tag,
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
    # 列表行要展示标签角标：预取关联与标签（含组名，用于标注"历史标签"），避免 N+1。
    queryset = queryset.prefetch_related("ticket_tags__tag__group")

    if scope == "mine":
        queryset = queryset.filter(assignee=request.user)
    elif scope == "unassigned":
        queryset = queryset.filter(assignee__isnull=True).exclude(status="closed")
    elif scope == "awaiting":
        queryset = queryset.filter(is_awaiting_reply=True).filter(
            Q(assignee__isnull=True) | Q(assignee=request.user)
        )

    form = InboxFilterForm(request.GET or None)
    filters = {"q": "", "group": "", "status": "", "awaiting": "", "assignee": "", "tag": ""}
    if form.is_valid():
        data = form.cleaned_data
        filters = {
            "q": data.get("q") or "",
            "group": data["group"].pk if data.get("group") else "",
            "status": data.get("status") or "",
            "awaiting": data.get("awaiting") or "",
            "assignee": data.get("assignee") or "",
            "tag": request.GET.get("tag", ""),
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
                "tag": request.GET.get("tag", ""),
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
        # 手工拼接的查询串可能不是合法主键（如 ?group=abc），这里做安全转换：
        # 非法值直接忽略该筛选，绝不因为一个坏参数返回 500。
        try:
            queryset = queryset.filter(group_id=int(filters["group"]))
        except (TypeError, ValueError):
            logger.info("收件箱收到非法的 group 参数：%r，已忽略。", filters["group"])
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
    if filters["tag"]:
        # 非法主键（如 ?tag=abc）直接忽略该筛选，绝不因为一个坏参数返回 500；
        # 过滤始终建立在 visible_tickets 之上，跨组标签不会泄露他人工单。
        try:
            queryset = queryset.filter(ticket_tags__tag_id=int(filters["tag"]))
        except (TypeError, ValueError):
            logger.info("收件箱收到非法的 tag 参数：%r，已忽略。", filters["tag"])

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
        "tag_options": _tag_filter_options(request.user),
    }
    template = "tickets/_inbox_table.html" if _is_htmx(request) else "tickets/inbox.html"
    return render(request, template, context)


def _tag_filter_options(user):
    """收件箱「标签」筛选下拉：全局标签 + 当前用户可见组的标签（去重、仅启用）。

    等价于对用户可见的每个组调用 `tags_available_for(group)` 后取并集。
    跨组管理员（sees_all_tickets）可看到全部标签。
    """
    queryset = Tag.objects.filter(is_active=True)
    if not getattr(user, "sees_all_tickets", False):
        group_ids = user.user_groups.values_list("group_id", flat=True)
        queryset = queryset.filter(Q(group__isnull=True) | Q(group_id__in=group_ids))
    return queryset.select_related("group").distinct().order_by("name", "id")


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
        "ticket_tags": ticket.ticket_tags.select_related("tag", "tag__group").order_by(
            "tag__name", "id"
        ),
        "tag_form": TicketTagForm(ticket=ticket),
        "max_tags": MAX_TAGS_PER_TICKET,
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
    if not attachment.is_previewable:
        raise PermissionDenied("该附件类型不允许在线预览，请下载后自行确认安全性。")
    return _serve_attachment(attachment, as_attachment=False)


def _serve_attachment(attachment: Attachment, *, as_attachment: bool):
    """在完成可见性校验后把附件发给客户端。

    存储型 XSS 防护（见 docs/安全清单核查.md R-3）：
    - 在线预览只允许白名单内的安全类型（PNG/JPEG/GIF/WebP/BMP/纯文本/PDF）；
      HTML、SVG、XHTML、XML 等"活动内容"以及未知类型一律只能下载；
    - 预览响应用白名单 MIME（而不是发件人可控的原始 MIME），并强制
      `X-Content-Type-Options: nosniff` 与 `Content-Security-Policy: default-src 'none'; sandbox`，
      即使内容被伪装成图片也无法在应用同源执行脚本。

    生产环境推荐配置 `ATTACHMENT_X_ACCEL_PREFIX`（如 `/media/attachments/`），
    由 Nginx 的 `internal` location 负责传输（X-Accel-Redirect），
    这样 `/media/` 目录不必对外暴露：未通过 Django 鉴权的直链一律 404。
    未配置时由 Django 直接流式返回（本地开发/单机部署可用）。
    """
    disposition = content_disposition_header(as_attachment, attachment.filename)
    prefix = getattr(settings, "ATTACHMENT_X_ACCEL_PREFIX", "") or ""

    if as_attachment:
        content_type = attachment.mime or "application/octet-stream"
    else:
        content_type = attachment.effective_mime or "application/octet-stream"

    if prefix:
        response = HttpResponse(content_type=content_type)
        response["Content-Disposition"] = disposition
        # 头部值必须是 ASCII：附件名可能含中文，这里做百分号编码，
        # 否则 Django 会按 RFC2047 编码整个头部，Nginx 无法解析。
        response["X-Accel-Redirect"] = f"{prefix.rstrip('/')}/{quote(attachment.path)}"
    else:
        try:
            handle = storage.absolute_path(attachment.path).open("rb")
        except (OSError, ValueError) as exc:
            raise Http404("附件文件已不存在。") from exc
        response = FileResponse(handle, as_attachment=as_attachment, filename=attachment.filename)
        response["Content-Type"] = content_type

    response["X-Content-Type-Options"] = "nosniff"
    if not as_attachment:
        response["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
        response["Referrer-Policy"] = "no-referrer"
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


# ------------------------------------------------------------------ 标签（v1.2 界面层）
@login_required
@require_POST
def add_ticket_tag(request, pk: int):
    """给工单打标签（下拉选现有标签，或输入新标签名）。写操作：POST + CSRF + PRG。"""
    ticket = get_visible_ticket(request.user, pk)
    form = TicketTagForm(request.POST, ticket=ticket)

    if not form.is_valid():
        messages.error(request, _tag_form_error(form))
        return _back_after_tag_change(request, ticket)

    try:
        link, created = add_tag(ticket, form.selected(), user=request.user, source="manual")
    except ValueError as exc:
        # 停用标签 / 跨组标签 / 超过单工单上限：服务层给出的中文提示直接展示，绝不 500。
        messages.error(request, str(exc))
    else:
        if created:
            messages.success(request, f"已添加标签「{link.tag.name}」。")
        else:
            messages.info(request, f"工单已有标签「{link.tag.name}」，未重复添加。")
    return _back_after_tag_change(request, ticket)


@login_required
@require_POST
def remove_ticket_tag(request, pk: int, tag_id: int):
    """移除工单标签（历史标签/已停用标签同样允许移除）。"""
    ticket = get_visible_ticket(request.user, pk)
    tag = get_object_or_404(Tag, pk=tag_id)
    try:
        removed = remove_tag(ticket, tag, user=request.user)
    except ValueError as exc:  # 服务层防御性抛错，同样转成提示
        messages.error(request, str(exc))
    else:
        if removed:
            messages.success(request, f"已移除标签「{tag.name}」。")
        else:
            messages.info(request, f"工单上没有标签「{tag.name}」。")
    return _back_after_tag_change(request, ticket)


@login_required
def tag_list(request):
    """标签总览（登录即可访问）：全局标签与各组标签、使用次数、创建时间。"""
    queryset = (
        Tag.objects.select_related("group")
        .annotate(usage_count=Count("ticket_tags", distinct=True))
        .order_by("group_id", "name", "id")
    )
    paginator = Paginator(queryset, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "tickets/tag_list.html",
        {"page_obj": page_obj, "tags": page_obj.object_list},
    )


def _tag_form_error(form) -> str:
    """把表单错误压成一条面向用户的中文提示。"""
    for errors in form.errors.values():
        for error in errors:
            return str(error)
    return "标签无效，请选择已有标签或输入新的标签名。"


def _back_after_tag_change(request, ticket: Ticket):
    """标签增删后的返回：HTMX 请求返回时间线局部（与 reply/note 一致），否则 PRG 重定向。"""
    if _is_htmx(request):
        return message_list(request, ticket.pk)
    return redirect("tickets:detail", pk=ticket.pk)
