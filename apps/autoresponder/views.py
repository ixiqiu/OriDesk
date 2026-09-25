"""自动回复模板视图（开发文档 §2.4 / §5.3 / §7）。

模板解析顺序（§5.3）：组覆盖 > 全局 > 代码内置默认模板。
- `template_edit` 同时服务 `autoresponder:template_edit`（scope=global 固定参数）
  与 `autoresponder:group_template_edit`（URL 带组 id）。
- URL 中的删除入口名为 `autoresponder:group_template_delete`（见 urls.py），
  只删除 `scope=group` 的覆盖模板，删除后该组自动回落全局模板。
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import Group
from apps.autoresponder.forms import TemplateForm
from apps.autoresponder.services import (
    DEFAULT_TEMPLATE,
    TEMPLATE_VARIABLES,
    render_template,
)
from apps.core.permissions import routing_manager_required
from apps.routing.models import Template

# 预览用示例上下文：必须与 services.template_context 的键一致（§5.3）
SAMPLE_TICKET_ID = 1234


def _sample_context(group: Group | None) -> dict:
    identity = group.identity_email if group is not None else None
    return {
        "ticket_id": SAMPLE_TICKET_ID,
        "ticket_no": f"[T#{SAMPLE_TICKET_ID}]",
        "subject": "示例：无法登录后台",
        "customer_email": "customer@example.com",
        "group_name": group.name if group is not None else "示例用户组",
        "identity_email": identity or "support@example.com",
        "date": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
    }


# ---------------------------------------------------------------------- 列表
@routing_manager_required
def template_list(request):
    global_template = Template.objects.filter(scope="global").order_by("id").first()
    templates_by_group = {
        tpl.group_id: tpl for tpl in Template.objects.filter(scope="group").order_by("id")
    }
    rows = [
        {"group": group, "template": templates_by_group.get(group.pk)}
        for group in Group.objects.all().order_by("name")
    ]
    context = {
        "global_template": global_template,
        "rows": rows,
        "variables": TEMPLATE_VARIABLES,
        "default_template": DEFAULT_TEMPLATE,
        "group_template_count": len(templates_by_group),
    }
    return render(request, "autoresponder/template_list.html", context)


# ---------------------------------------------------------------------- 编辑
@routing_manager_required
def template_edit(request, scope: str = "global", pk: int | None = None):
    """编辑全局模板（pk 为空）或某个组的覆盖模板（pk 为组 id）。"""
    if pk is not None:
        scope = "group"
        group = get_object_or_404(Group, pk=pk)
    else:
        scope = "global"
        group = None

    existing = _existing_template(scope, group)
    fallback_body = existing.body if existing else DEFAULT_TEMPLATE

    if request.method == "POST":
        form = TemplateForm(request.POST)
        if form.is_valid():
            body = form.cleaned_data["body"]
            candidate = Template(scope=scope, group=group, body=body)
            try:
                # 先校验模型的 scope/group 配套约束（§10.1 模型先行）
                candidate.clean()
            except ValidationError as exc:
                for error in exc.messages:
                    form.add_error("body", error)
            else:
                if "_preview" not in request.POST:
                    Template.objects.update_or_create(
                        scope=scope, group=group, defaults={"body": body}
                    )
                    if group is None:
                        messages.success(request, "全局自动回复模板已保存。")
                    else:
                        messages.success(
                            request,
                            f"「{group.name}」的专属模板已保存，该组将不再使用全局模板。",
                        )
                    return redirect("autoresponder:template_list")
                messages.info(request, "预览已刷新，尚未保存。确认无误后点击「保存模板」。")
    else:
        form = TemplateForm(initial={"body": fallback_body})

    if request.method == "POST":
        source_body = (
            form.cleaned_data["body"] if form.is_valid() else (request.POST.get("body") or "")
        )
    else:
        source_body = fallback_body

    context = {
        "form": form,
        "scope": scope,
        "group": group,
        "existing": existing,
        "preview": render_template(source_body, _sample_context(group)),
        "variables": TEMPLATE_VARIABLES,
        "default_template": DEFAULT_TEMPLATE,
        "title": "全局自动回复模板" if group is None else f"「{group.name}」专属自动回复模板",
    }
    return render(request, "autoresponder/template_form.html", context)


def _existing_template(scope: str, group: Group | None) -> Template | None:
    if scope == "group" and group is not None:
        return Template.objects.filter(scope="group", group=group).order_by("id").first()
    return Template.objects.filter(scope="global").order_by("id").first()


# ---------------------------------------------------------------------- 删除组覆盖
@routing_manager_required
@require_POST
def template_delete(request, pk: int):
    """删除 `scope=group` 的覆盖模板，该组回落到全局模板。"""
    group = get_object_or_404(Group, pk=pk)
    template = Template.objects.filter(scope="group", group=group).first()
    if template is None:
        messages.error(request, f"「{group.name}」当前没有专属模板，无需删除。")
    else:
        template.delete()
        messages.success(request, f"已删除「{group.name}」的专属模板，该组将回落到全局模板。")
    return redirect("autoresponder:template_list")
