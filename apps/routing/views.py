"""路由规则 / 系统设置 / 邮箱总览视图（开发文档 §4.4 / §4.6 / §7）。

约定（docs/UI接口约定.md §5）：
- 全部视图使用 `@routing_manager_required`；写操作一律 `@require_POST` + CSRF。
- 成功后 `messages` 反馈并重定向（PRG）。
- 规则试算是**只读**操作，绝不写入任何数据。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import Group, Mailbox
from apps.audit.models import Setting
from apps.core.permissions import routing_manager_required
from apps.mailboxes.tasks import dispatch, sync_mailbox_task
from apps.routing.forms import RuleForm, RuleTrialForm, SystemSettingsForm, TagForm
from apps.routing.models import Rule
from apps.routing.services import (
    RoutingError,
    admin_group,
    entry_kind,
    match_rule,
    recent_ticket_for_sender,
    route_ticket,
    sticky_window_days,
)
from apps.tickets.models import Tag

logger = logging.getLogger(__name__)

PAGE_SIZE = 25

ENTRY_KIND_LABELS = {
    "group": "组专用",
    "fallback": "全局兜底",
    "unified": "统一进线",
}


# ---------------------------------------------------------------------- 规则列表
@routing_manager_required
def rule_list(request):
    """规则列表 + 规则试算小工具（试算 POST 到本视图自身）。"""
    queryset = Rule.objects.select_related("mailbox").order_by("priority", "id")
    paginator = Paginator(queryset, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    trial_form = RuleTrialForm(request.POST) if request.method == "POST" else RuleTrialForm()
    trial_result = None
    if request.method == "POST" and trial_form.is_valid():
        trial_result = _run_trial(trial_form)

    context = {
        "page_obj": page_obj,
        "rules": page_obj.object_list,
        "trial_form": trial_form,
        "trial_result": trial_result,
        "sticky_days": sticky_window_days(),
    }
    return render(request, "routing/rule_list.html", context)


def _run_trial(form: RuleTrialForm) -> dict:
    """只读试算：匹配规则并给出最终归属组，不写任何数据。"""
    mailbox = form.cleaned_data["mailbox"]
    msg = form.build_message()
    sender = form.sender_address()
    kind = entry_kind(mailbox)

    result: dict = {
        "mailbox": mailbox,
        "kind": kind,
        "kind_label": ENTRY_KIND_LABELS.get(kind, kind),
        "sender": sender,
        "matched_rule": None,
        "group": None,
        "owning_group": mailbox.owning_group,
        "sticky_ticket": None,
        "sticky_window_days": sticky_window_days(),
        "error": "",
    }

    result["matched_rule"] = match_rule(msg, mailbox)

    if sender:
        last = recent_ticket_for_sender(sender)
        window = timedelta(days=result["sticky_window_days"])
        if last and last.last_message_at and last.last_message_at >= timezone.now() - window:
            result["sticky_ticket"] = last

    try:
        result["group"] = route_ticket(msg, sender, mailbox, now=timezone.now())
    except RoutingError as exc:
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - 试算不得把页面打成 500
        logger.exception("规则试算出现未预期错误。")
        result["error"] = f"试算失败：{exc}"
    return result


# ---------------------------------------------------------------------- 规则增删改
@routing_manager_required
def rule_create(request):
    if request.method == "POST":
        form = RuleForm(request.POST)
        if form.is_valid():
            rule = form.save()
            messages.success(request, f"规则「{rule}」已创建。")
            return redirect("routing:rule_list")
    else:
        form = RuleForm(
            initial={
                "priority": 100,
                "enabled": True,
                "match_field": "subject",
                "match_op": "contains",
                "action_type": "assign_group",
            }
        )
    return render(request, "routing/rule_form.html", {"form": form, "rule": None})


@routing_manager_required
def rule_edit(request, pk: int):
    rule = get_object_or_404(Rule.objects.select_related("mailbox"), pk=pk)
    if request.method == "POST":
        form = RuleForm(request.POST, instance=rule)
        if form.is_valid():
            rule = form.save()
            messages.success(request, f"规则「{rule}」已保存。")
            return redirect("routing:rule_list")
    else:
        form = RuleForm(instance=rule)
    return render(request, "routing/rule_form.html", {"form": form, "rule": rule})


@routing_manager_required
@require_POST
def rule_delete(request, pk: int):
    rule = get_object_or_404(Rule, pk=pk)
    label = str(rule)
    rule.delete()
    messages.success(request, f"规则已删除：{label}")
    return redirect("routing:rule_list")


@routing_manager_required
@require_POST
def rule_toggle(request, pk: int):
    rule = get_object_or_404(Rule, pk=pk)
    rule.enabled = not rule.enabled
    rule.save(update_fields=["enabled"])
    state = "启用" if rule.enabled else "停用"
    messages.success(request, f"规则「{rule}」已{state}。")
    return redirect("routing:rule_list")


# ---------------------------------------------------------------------- 系统设置
@routing_manager_required
def settings_edit(request):
    if request.method == "POST":
        form = SystemSettingsForm(request.POST)
        if form.is_valid():
            keys = form.save(user=request.user)
            messages.success(
                request,
                f"系统设置已保存（{len(keys)} 项），变更已写入审计日志。",
            )
            return redirect("routing:settings")
    else:
        form = SystemSettingsForm()

    fallback_group_id = Setting.get_optional_int("fallback_group_id")
    fallback_group_obj = (
        Group.objects.filter(pk=fallback_group_id).first() if fallback_group_id else None
    )
    fallback_mailbox_id = Setting.get_optional_int("fallback_mailbox_id")
    fallback_mailbox_obj = (
        Mailbox.objects.filter(pk=fallback_mailbox_id).first() if fallback_mailbox_id else None
    )

    context = {
        "form": form,
        "fallback_group": fallback_group_obj,
        "fallback_mailbox": fallback_mailbox_obj,
        "effective_fallback_mailbox": Mailbox.get_fallback(),
        "admin_group": admin_group(),
        "defaults": Setting.DEFAULTS,
        "sticky_days": Setting.get_int("sticky_window_days", 7),
        "first_contact_hours": Setting.get_int("first_contact_window_hours", 24),
        "max_attachment_mb": Setting.get_int("max_attachment_size_mb", 25),
        "imap_poll_seconds": Setting.get_int("imap_poll_interval_seconds", 60),
        # 令牌**不回显**，只告诉管理员"配没配"。表单里那个密码框留空即保持不变。
        "ntfy_token_set": bool(Setting.get("ntfy_token")),
    }
    return render(request, "routing/settings.html", context)


# ---------------------------------------------------------------------- 邮箱总览
@routing_manager_required
def mailbox_overview(request):
    if request.method == "POST":
        return _sync_mailbox(request)

    rows = []
    for mailbox in (
        Mailbox.objects.select_related("group").annotate(rule_count=Count("rules")).order_by("id")
    ):
        group = mailbox.owning_group
        kind = entry_kind(mailbox)
        rows.append(
            {
                "mailbox": mailbox,
                "kind": kind,
                "kind_label": ENTRY_KIND_LABELS.get(kind, ""),
                "group": group,
                "identity_email": (
                    group.identity_email if group else (mailbox.email if mailbox.is_fallback else None)
                ),
                "rule_count": mailbox.rule_count,
            }
        )
    return render(request, "routing/mailbox_overview.html", {"rows": rows})


def _sync_mailbox(request):
    """手动触发单个邮箱的 IMAP 增量同步；任何异常都转成提示，绝不 500。"""
    raw_id = (request.POST.get("mailbox_id") or "").strip()
    mailbox = Mailbox.objects.filter(pk=int(raw_id)).first() if raw_id.isdigit() else None
    if mailbox is None:
        messages.error(request, "未找到要同步的邮箱，请刷新页面后重试。")
        return redirect("routing:mailbox_overview")

    try:
        mode, result = dispatch(sync_mailbox_task, mailbox.pk)
    except Exception as exc:  # noqa: BLE001 - 手动同步失败必须可感知但不影响页面
        logger.exception("邮箱 %s 手动同步失败。", mailbox.email)
        messages.error(request, f"邮箱 {mailbox.email} 同步失败：{exc}")
        return redirect("routing:mailbox_overview")

    if mode == "queued":
        messages.success(
            request,
            f"邮箱 {mailbox.email} 的同步任务已加入队列，稍后刷新本页查看增量位点变化。",
        )
        return redirect("routing:mailbox_overview")

    stats = result if isinstance(result, dict) else {}
    if stats.get("error"):
        messages.error(request, f"邮箱 {mailbox.email} 同步未完成：{stats['error']}")
    else:
        messages.success(
            request,
            f"邮箱 {mailbox.email} 同步完成（同步执行）：拉取 {stats.get('fetched', 0)} 封，"
            f"入库 {stats.get('processed', 0)} 封，跳过 {stats.get('skipped', 0)} 封，"
            f"失败 {stats.get('failed', 0)} 封。",
        )
    return redirect("routing:mailbox_overview")


# ------------------------------------------------------------------ 标签字典管理（v1.2）
@routing_manager_required
def tag_admin_list(request):
    """标签字典总览：含停用标签，展示作用域 / 颜色 / 关联工单数。"""
    queryset = (
        Tag.objects.select_related("group")
        .annotate(usage_count=Count("ticket_tags", distinct=True))
        .order_by("group_id", "name", "id")
    )
    paginator = Paginator(queryset, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "routing/tag_list.html",
        {"page_obj": page_obj, "tags": page_obj.object_list},
    )


@routing_manager_required
def tag_admin_edit(request, pk: int | None = None):
    """标签新建（无 pk）/ 编辑（有 pk）。同名同作用域由 Tag.clean() 校验后转表单错误。"""
    tag = get_object_or_404(Tag.objects.select_related("group"), pk=pk) if pk is not None else None
    editing = tag is not None

    if request.method == "POST":
        form = TagForm(request.POST, instance=tag)
        if form.is_valid():
            tag = form.save()
            messages.success(
                request,
                f"标签「{tag.name}」（{tag.scope_label}）已{'保存' if editing else '创建'}。",
            )
            return redirect("routing:tag_admin_list")
    else:
        form = TagForm(instance=tag)

    return render(request, "routing/tag_form.html", {"form": form, "tag": tag, "editing": editing})


@routing_manager_required
@require_POST
def tag_admin_delete(request, pk: int):
    """删除标签。会级联删除所有工单上的关联，页面已二次确认并明确提示。"""
    tag = get_object_or_404(Tag, pk=pk)
    label = str(tag)
    linked = tag.ticket_tags.count()
    tag.delete()
    messages.success(request, f"标签已删除：{label}（同时解除了 {linked} 个工单关联）。")
    return redirect("routing:tag_admin_list")


@routing_manager_required
@require_POST
def tag_admin_toggle(request, pk: int):
    """停用 / 启用标签（推荐用停用代替删除，保留历史关联）。"""
    tag = get_object_or_404(Tag, pk=pk)
    tag.is_active = not tag.is_active
    tag.save(update_fields=["is_active"])
    messages.success(request, f"标签「{tag.name}」已{'启用' if tag.is_active else '停用'}。")
    return redirect("routing:tag_admin_list")
