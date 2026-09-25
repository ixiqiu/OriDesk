"""工单管理后台。"""

from __future__ import annotations

from django.contrib import admin, messages
from django.utils import timezone

from apps.tickets.models import Attachment, Message, Ticket
from apps.tickets.services import claim_ticket, set_status, unclaim_ticket


class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 0
    fields = ("filename", "mime", "size", "path", "is_dangerous_display")
    readonly_fields = ("filename", "mime", "size", "path", "is_dangerous_display")

    @admin.display(description="可预览")
    def is_dangerous_display(self, obj):
        return "否（危险类型）" if obj.is_dangerous else "是"

    def has_add_permission(self, request, obj=None):
        return False


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("created_at", "direction", "type", "from_addr", "to_addr", "subject", "is_auto_reply", "actual_sender")
    readonly_fields = fields
    ordering = ("created_at",)

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "subject",
        "customer_email",
        "group",
        "assignee",
        "status",
        "is_awaiting_reply",
        "last_message_at",
    )
    list_filter = ("group", "status", "is_awaiting_reply", "mailbox")
    search_fields = ("subject", "normalized_subject", "customer_email")
    date_hierarchy = "last_message_at"
    autocomplete_fields = ("assignee",)
    inlines = [MessageInline]
    readonly_fields = ("created_at", "updated_at", "last_message_at")
    actions = ["action_claim_to_me", "action_unclaim", "action_mark_awaiting", "action_close", "action_reopen"]

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.last_message_at = timezone.now()
        super().save_model(request, obj, form, change)

    # ---- 批量操作 ----
    @admin.action(description="认领给当前管理员")
    def action_claim_to_me(self, request, queryset):
        for ticket in queryset:
            claim_ticket(ticket, request.user)
        self.message_user(request, f"已认领 {queryset.count()} 个工单。", messages.SUCCESS)

    @admin.action(description="取消认领")
    def action_unclaim(self, request, queryset):
        for ticket in queryset:
            if ticket.assignee_id:
                unclaim_ticket(ticket, request.user)
        self.message_user(request, "已取消认领。", messages.SUCCESS)

    @admin.action(description="标记为待回复")
    def action_mark_awaiting(self, request, queryset):
        updated = queryset.update(is_awaiting_reply=True)
        self.message_user(request, f"已标记 {updated} 个工单为待回复。", messages.SUCCESS)

    @admin.action(description="关闭工单")
    def action_close(self, request, queryset):
        for ticket in queryset:
            set_status(ticket, "closed", user=request.user)
        self.message_user(request, "已关闭所选工单。", messages.SUCCESS)

    @admin.action(description="重新打开工单")
    def action_reopen(self, request, queryset):
        for ticket in queryset:
            set_status(ticket, "open", user=request.user)
        self.message_user(request, "已重新打开所选工单。", messages.SUCCESS)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "ticket",
        "created_at",
        "direction",
        "type",
        "from_addr",
        "to_addr",
        "is_auto_reply",
        "actual_sender",
    )
    list_filter = ("direction", "type", "is_auto_reply", "mailbox")
    search_fields = ("subject", "from_addr", "to_addr", "message_id", "body_text")
    date_hierarchy = "created_at"
    inlines = [AttachmentInline]
    readonly_fields = ("created_at",)

    def has_add_permission(self, request):
        # 内部备注请走 Web 界面（apps.tickets.services.add_note），保证时间线与审计一致
        return False


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("id", "filename", "mime", "size", "is_dangerous", "message", "created_at")
    list_filter = ("mime",)
    search_fields = ("filename", "path")
    readonly_fields = ("created_at",)

    def has_add_permission(self, request):
        return False
