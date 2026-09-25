"""账号与邮箱管理界面（开发文档 §2.1 / §2.7 / §5.6 / §10.3）。

约定（与 apps/tickets/views.py 保持一致）：
- 所有视图都有权限装饰器（`@login_required` / `@superadmin_required`）。
- 写操作一律 POST + CSRF，成功后 `messages` 反馈并 PRG 重定向。
- 列表页统一分页（25 条/页），不整表渲染。
- 邮箱凭据加密存储：页面不回显、日志不出现明文；连通性检查只读，绝不发送邮件。
"""

from __future__ import annotations

import logging
import smtplib

import imapclient
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.forms import GroupForm, MailboxForm, UserForm
from apps.accounts.models import Group, Mailbox, User, UserGroup
from apps.core.permissions import superadmin_required
from apps.core.utils import truncate
from apps.tickets.selectors import visible_tickets

logger = logging.getLogger(__name__)

PAGE_SIZE = 25


# ------------------------------------------------------------------ 我的账号
@login_required
def profile(request):
    """个人资料：账号信息、所属组与组内身份、可见工单数、改密/退出入口。"""
    memberships = list(
        UserGroup.objects.filter(user=request.user)
        .select_related("group", "group__mailbox")
        .order_by("group__name")
    )
    context = {
        "memberships": memberships,
        "admin_memberships": [item for item in memberships if item.is_admin],
        "visible_ticket_count": visible_tickets(request.user).count(),
        "profile_user": request.user,
    }
    return render(request, "accounts/profile.html", context)


# ------------------------------------------------------------------ 用户管理
@superadmin_required
def user_list(request):
    """用户列表：用户名/邮箱搜索 + 按组筛选 + 分页。"""
    queryset = User.objects.all().prefetch_related("user_groups__group").order_by("username", "pk")

    query = (request.GET.get("q") or "").strip()
    group_id = (request.GET.get("group") or "").strip()
    if query:
        queryset = queryset.filter(
            Q(username__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
        )
    if group_id.isdigit():
        queryset = queryset.filter(user_groups__group_id=int(group_id)).distinct()

    page_obj = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "page_obj": page_obj,
        "users": page_obj.object_list,
        "groups": Group.objects.all().order_by("name"),
        "filters": {"q": query, "group": group_id},
    }
    return render(request, "accounts/user_list.html", context)


@superadmin_required
def user_create(request):
    """新建用户。"""
    form = UserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        messages.success(request, f"用户「{user.display_name}」已创建。")
        return redirect("accounts:user_list")
    if request.method == "POST":
        messages.error(request, "用户未保存，请检查表单中的错误。")
    return render(
        request,
        "accounts/user_form.html",
        {"form": form, "is_create": True, "object": None},
    )


@superadmin_required
def user_edit(request, pk: int):
    """编辑用户（密码留空表示不修改）。"""
    user = get_object_or_404(User, pk=pk)
    form = UserForm(request.POST or None, instance=user)
    if request.method == "POST" and form.is_valid():
        password_changed = bool(form.cleaned_data.get("password"))
        form.save()
        if password_changed and request.user.pk == user.pk:
            # 修改自己的密码后保持当前会话有效，避免把自己踢下线
            update_session_auth_hash(request, user)
        messages.success(request, f"用户「{user.display_name}」已保存。")
        return redirect("accounts:user_list")
    if request.method == "POST":
        messages.error(request, "用户未保存，请检查表单中的错误。")
    return render(
        request,
        "accounts/user_form.html",
        {"form": form, "is_create": False, "object": user},
    )


# ------------------------------------------------------------------ 用户组管理
@superadmin_required
def group_list(request):
    """用户组列表：邮箱/身份邮箱、成员数、工单数。"""
    queryset = (
        Group.objects.select_related("mailbox")
        .annotate(
            member_count=Count("user_groups", distinct=True),
            ticket_count=Count("tickets", distinct=True),
        )
        .order_by("name")
    )
    page_obj = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "page_obj": page_obj,
        "groups": page_obj.object_list,
        "has_admin_group": Group.objects.filter(is_admin_group=True).exists(),
        "has_fallback_mailbox": Mailbox.objects.filter(is_fallback=True).exists(),
    }
    return render(request, "accounts/group_list.html", context)


@superadmin_required
def group_create(request):
    """新建用户组（含成员与组内管理员）。"""
    form = GroupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        group = form.save()
        messages.success(request, f"用户组「{group.name}」已创建。")
        return redirect("accounts:group_list")
    if request.method == "POST":
        messages.error(request, "用户组未保存，请检查表单中的错误。")
    return render(
        request,
        "accounts/group_form.html",
        {"form": form, "is_create": True, "object": None},
    )


@superadmin_required
def group_edit(request, pk: int):
    """编辑用户组。"""
    group = get_object_or_404(Group, pk=pk)
    form = GroupForm(request.POST or None, instance=group)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"用户组「{group.name}」已保存。")
        return redirect("accounts:group_list")
    if request.method == "POST":
        messages.error(request, "用户组未保存，请检查表单中的错误。")
    return render(
        request,
        "accounts/group_form.html",
        {"form": form, "is_create": False, "object": group},
    )


# ------------------------------------------------------------------ 邮箱配置
@superadmin_required
def mailbox_list(request):
    """邮箱列表：连接参数与同步进度；凭据只显示是否已配置。"""
    queryset = Mailbox.objects.select_related("group").order_by("name", "id")
    page_obj = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))
    mailboxes = list(page_obj.object_list)
    for mailbox in mailboxes:
        # 布尔标记，模板据此渲染「已配置 / 未配置」，永不输出密文。
        mailbox.has_secret = bool(mailbox.secret_encrypted)
    context = {
        "page_obj": page_obj,
        "mailboxes": mailboxes,
        "has_fallback_mailbox": Mailbox.objects.filter(is_fallback=True).exists(),
    }
    return render(request, "accounts/mailbox_list.html", context)


@superadmin_required
def mailbox_create(request):
    """新建邮箱配置（必须填写凭据）。"""
    form = MailboxForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        mailbox = form.save()
        messages.success(request, f"邮箱「{mailbox.email}」已创建。")
        return redirect("accounts:mailbox_list")
    if request.method == "POST":
        messages.error(request, "邮箱未保存，请检查表单中的错误。")
    return render(
        request,
        "accounts/mailbox_form.html",
        {"form": form, "is_create": True, "object": None},
    )


@superadmin_required
def mailbox_edit(request, pk: int):
    """编辑邮箱配置（凭据留空表示不修改）。"""
    mailbox = get_object_or_404(Mailbox, pk=pk)
    form = MailboxForm(request.POST or None, instance=mailbox)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"邮箱「{mailbox.email}」已保存。")
        return redirect("accounts:mailbox_list")
    if request.method == "POST":
        messages.error(request, "邮箱未保存，请检查表单中的错误。")
    return render(
        request,
        "accounts/mailbox_form.html",
        {"form": form, "is_create": False, "object": mailbox},
    )


@superadmin_required
@require_POST
def mailbox_verify(request, pk: int):
    """只读连通性检查：IMAP 登录并选中 INBOX，SMTP 登录后立即退出（不发信）。"""
    mailbox = get_object_or_404(Mailbox, pk=pk)

    try:
        secret = mailbox.get_secret()
    except Exception as exc:  # noqa: BLE001 - CredentialError 等一律转为可读提示
        logger.warning("邮箱 #%s 凭据读取失败：%s", mailbox.pk, type(exc).__name__)
        messages.error(request, f"凭据读取失败，请重新录入授权码：{_safe_error(exc)}")
        return redirect("accounts:mailbox_list")

    try:
        inbox_count = _imap_inbox_count(mailbox, secret)
        _smtp_login_check(mailbox, secret)
    except Exception as exc:  # noqa: BLE001 - 网络/认证异常统一反馈，绝不 500
        logger.warning(
            "邮箱 #%s 连通性检查失败：%s: %s",
            mailbox.pk,
            type(exc).__name__,
            _safe_error(exc, secret),
        )
        messages.error(request, f"连通性检查失败：{_safe_error(exc, secret)}")
    else:
        messages.success(
            request,
            f"IMAP 登录成功，INBOX 共 {inbox_count} 封；SMTP 登录成功（仅登录校验，未发送任何邮件）。",
        )
    return redirect("accounts:mailbox_list")


def _imap_inbox_count(mailbox: Mailbox, secret: str) -> int:
    """只读登录 IMAP 并选中 INBOX，返回邮件总数。"""
    client = imapclient.IMAPClient(mailbox.imap_host, port=mailbox.imap_port, ssl=mailbox.imap_ssl)
    try:
        client.login(mailbox.username, secret)
        selected = client.select_folder("INBOX")
        if isinstance(selected, dict):
            return int(selected.get(b"EXISTS", 0) or 0)
        return 0
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001, S110 - 关闭失败不影响检查结论
            pass


def _smtp_login_check(mailbox: Mailbox, secret: str) -> None:
    """SMTP 登录校验：建立连接 + login() + quit()，不调用任何 sendmail。"""
    if mailbox.smtp_ssl:
        server = smtplib.SMTP_SSL(mailbox.smtp_host, mailbox.smtp_port, timeout=10)
    else:
        server = smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=10)
    try:
        server.login(mailbox.username, secret)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001, S110 - 退出失败不影响登录校验结论
            pass


def _safe_error(exc: Exception, secret: str | None = None) -> str:
    """异常摘要：截断并抹掉可能出现的凭据明文，绝不写日志/页面明文。"""
    text = f"{type(exc).__name__}: {exc}"
    if secret:
        text = text.replace(secret, "***")
    return truncate(text, 200)
