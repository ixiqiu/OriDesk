from django.contrib import admin

from apps.audit.models import AuditLog, Setting


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "user", "ticket", "group", "identity_email")
    list_filter = ("action", "created_at")
    search_fields = ("identity_email", "detail")
    date_hierarchy = "created_at"
    readonly_fields = tuple(field.name for field in AuditLog._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Setting)
class SettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value", "updated_at")
    search_fields = ("key", "value")
