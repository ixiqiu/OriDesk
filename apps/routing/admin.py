"""路由规则与自动回复模板管理后台。"""

from __future__ import annotations

from django import forms
from django.contrib import admin
from django.utils.html import format_html

from apps.autoresponder.services import DEFAULT_TEMPLATE, TEMPLATE_VARIABLES, render_template
from apps.routing.models import Rule, Template


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ("priority", "mailbox", "match_summary", "action_summary", "enabled", "created_at")
    list_filter = ("enabled", "match_field", "match_op", "action_type", "mailbox")
    search_fields = ("match_value", "action_value")
    ordering = ("priority", "id")
    autocomplete_fields = ("mailbox",)
    list_editable = ("enabled",)
    fieldsets = (
        ("作用范围", {"fields": ("mailbox", "priority", "enabled")}),
        ("匹配条件", {"fields": ("match_field", "match_op", "match_value")}),
        ("命中动作", {"fields": ("action_type", "action_value")}),
    )

    @admin.display(description="匹配")
    def match_summary(self, obj):
        return f"{obj.get_match_field_display()} {obj.get_match_op_display()} {obj.match_value}"

    @admin.display(description="动作")
    def action_summary(self, obj):
        return f"{obj.get_action_type_display()} → {obj.action_value}"


class TemplateAdminForm(forms.ModelForm):
    class Meta:
        model = Template
        fields = ["scope", "group", "body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 14, "cols": 90})}
        help_texts = {
            "body": "支持变量：" + "、".join("{{ %s }}" % name for name in TEMPLATE_VARIABLES),
        }

    def clean_body(self):
        body = self.cleaned_data["body"]
        # 试渲染一次，语法错误在保存阶段就暴露（渲染失败时服务层会退回原文）
        render_template(
            body,
            {
                "ticket_id": 1,
                "ticket_no": "[T#1]",
                "subject": "示例主题",
                "customer_email": "customer@example.com",
                "group_name": "示例组",
                "identity_email": "group@example.com",
                "date": "2026-01-01 00:00",
            },
        )
        return body


@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    form = TemplateAdminForm
    list_display = ("scope", "group", "body_preview", "updated_at")
    list_filter = ("scope",)
    search_fields = ("body",)
    autocomplete_fields = ("group",)
    actions = ["action_install_default"]

    @admin.display(description="正文预览")
    def body_preview(self, obj):
        text = (obj.body or "").strip().replace("\n", " ")
        return format_html("<span title='{}'>{}</span>", text, text[:60] + ("…" if len(text) > 60 else ""))

    @admin.action(description="填入内置默认模板内容")
    def action_install_default(self, request, queryset):
        updated = queryset.update(body=DEFAULT_TEMPLATE)
        self.message_user(request, f"已更新 {updated} 个模板为内置默认内容。")
