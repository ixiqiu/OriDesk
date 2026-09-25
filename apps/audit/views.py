"""审计日志视图（开发文档 §2.5 / §4.5 / §10.3）。

- 只读列表，分页 50 条/页，支持动作 / 操作人 / 工单号 / 起止日期 / 关键字筛选。
- 关键字同时匹配 `identity_email` 与 `detail`（JSON 文本）。
- 导出 CSV 沿用当前筛选条件，输出带 UTF-8 BOM 的 CSV（Excel 直接打开不乱码）。
- 工单链接只在当前用户可见时渲染（§5.6 可见性），否则退化为纯文本。
"""

from __future__ import annotations

import csv
import json
import re

from django import forms
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.permissions import routing_manager_required
from apps.tickets.selectors import visible_tickets

PAGE_SIZE = 50
CSV_HEADERS = ["时间", "动作", "操作人", "工单号", "组", "对外身份", "摘要"]


class AuditLogFilterForm(forms.Form):
    """审计日志筛选器（GET 形式，无需 CSRF）。

    放在 views.py 内，因为本次改动只允许修改 audit 的 views / tests 两个文件。
    """

    action = forms.ChoiceField(
        choices=[("", "全部动作")] + AuditLog.ACTION_CHOICES,
        required=False,
        label="动作",
    )
    user = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="全部操作人",
        label="操作人",
    )
    ticket_no = forms.CharField(required=False, label="工单号")
    date_from = forms.DateField(
        required=False,
        label="开始日期",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    date_to = forms.DateField(
        required=False,
        label="结束日期",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    q = forms.CharField(
        required=False,
        label="关键字",
        widget=forms.TextInput(attrs={"placeholder": "对外身份 / 明细内容"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = (
            User.objects.filter(audit_logs__isnull=False).distinct().order_by("username")
        )


# ---------------------------------------------------------------------- 列表 / 导出
@routing_manager_required
def log_list(request):
    form = AuditLogFilterForm(request.GET or None)
    queryset = _filtered_logs(form)

    if request.GET.get("export") == "csv":
        return _csv_response(queryset)

    paginator = Paginator(queryset, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    logs = list(page_obj.object_list)
    visible_ids = _visible_ticket_ids(request.user, logs)
    for log in logs:
        log.ticket_visible = bool(log.ticket_id and log.ticket_id in visible_ids)
        log.summary = _summary(log)

    context = {
        "form": form,
        "page_obj": page_obj,
        "logs": logs,
        "filters": _filter_values(request.GET),
        "page_size": PAGE_SIZE,
    }
    return render(request, "audit/log_list.html", context)


def _filter_values(params) -> dict:
    return {
        "action": params.get("action", ""),
        "user": params.get("user", ""),
        "ticket_no": params.get("ticket_no", ""),
        "date_from": params.get("date_from", ""),
        "date_to": params.get("date_to", ""),
        "q": params.get("q", ""),
    }


def _filtered_logs(form: AuditLogFilterForm):
    queryset = AuditLog.objects.select_related("user", "ticket", "group")
    if not form.is_valid():
        return queryset.order_by("-created_at", "-id")

    data = form.cleaned_data
    if data.get("action"):
        queryset = queryset.filter(action=data["action"])
    if data.get("user"):
        queryset = queryset.filter(user=data["user"])

    ticket_no = (data.get("ticket_no") or "").strip()
    if ticket_no:
        digits = re.sub(r"\D", "", ticket_no)
        queryset = queryset.filter(ticket_id=int(digits)) if digits else queryset.none()

    if data.get("date_from"):
        queryset = queryset.filter(created_at__date__gte=data["date_from"])
    if data.get("date_to"):
        queryset = queryset.filter(created_at__date__lte=data["date_to"])

    keyword = (data.get("q") or "").strip()
    if keyword:
        queryset = queryset.filter(
            Q(identity_email__icontains=keyword) | Q(detail__icontains=keyword)
        )
    return queryset.order_by("-created_at", "-id")


def _visible_ticket_ids(user, logs) -> set[int]:
    ids = {log.ticket_id for log in logs if log.ticket_id}
    if not ids:
        return set()
    return set(visible_tickets(user).filter(pk__in=ids).values_list("pk", flat=True))


def _summary(log: AuditLog) -> str:
    detail = log.detail or {}
    if not isinstance(detail, dict) or not detail:
        return ""
    parts: list[str] = []
    for key in ("event", "key", "value", "subject", "to", "from_group", "to_group", "rule_id"):
        if key in detail and detail[key] not in (None, ""):
            parts.append(f"{key}={detail[key]}")
    if not parts:
        parts = [f"{key}={value}" for key, value in list(detail.items())[:3]]
    text = "，".join(str(part) for part in parts)
    return text[:120] + ("…" if len(text) > 120 else "")


def _csv_safe(value) -> str:
    r"""防 CSV 注入：以 = + - @ 制表符/回车开头的单元格在 Excel 中会被当公式执行。

    审计明细里可能含有客户可控内容（如邮件主题），因此统一加单引号前缀。
    """
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _csv_response(queryset) -> HttpResponse:
    stamp = timezone.localdate().strftime("%Y%m%d")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="audit-logs-{stamp}.csv"'
    response.write("\ufeff")  # UTF-8 BOM：Excel 打开中文不乱码

    writer = csv.writer(response)
    writer.writerow(CSV_HEADERS)
    for log in queryset.iterator():
        writer.writerow(
            [
                timezone.localtime(log.created_at).strftime("%Y-%m-%d %H:%M:%S"),
                _csv_safe(log.get_action_display()),
                _csv_safe(log.user.display_name if log.user else "系统"),
                f"[T#{log.ticket_id}]" if log.ticket_id else "",
                _csv_safe(log.group.name if log.group else ""),
                _csv_safe(log.identity_email or ""),
                _csv_safe(_summary(log)),
            ]
        )
    return response


# ---------------------------------------------------------------------- 详情
@routing_manager_required
def log_detail(request, pk: int):
    log = get_object_or_404(
        AuditLog.objects.select_related("user", "ticket", "group"), pk=pk
    )
    detail = log.detail if isinstance(log.detail, dict) else {}
    context = {
        "log": log,
        "detail_json": json.dumps(detail, ensure_ascii=False, indent=2, default=str),
        "ticket_visible": bool(
            log.ticket_id and visible_tickets(request.user).filter(pk=log.ticket_id).exists()
        ),
    }
    return render(request, "audit/log_detail.html", context)
