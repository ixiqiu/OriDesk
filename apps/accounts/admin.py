"""accounts 管理后台（开发文档 §7-6：用户、组、邮箱、成员管理）。"""

from __future__ import annotations

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.core.exceptions import ValidationError

from apps.accounts.models import Group, Mailbox, User, UserGroup


class MailboxAdminForm(forms.ModelForm):
    """邮箱表单：凭据以密码框录入，落库前 Fernet 加密，永不回显明文。"""

    secret = forms.CharField(
        label="授权码 / 密码",
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="留空表示不修改已保存的凭据；保存后仅以密文形式存储。",
    )

    class Meta:
        model = Mailbox
        fields = [
            "name",
            "email",
            "imap_host",
            "imap_port",
            "imap_ssl",
            "smtp_host",
            "smtp_port",
            "smtp_ssl",
            "username",
            "is_fallback",
            "is_active",
        ]

    def clean(self):
        cleaned = super().clean()
        if not self.instance.pk and not cleaned.get("secret"):
            raise ValidationError({"secret": "新建邮箱必须填写授权码/密码。"})
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        secret = self.cleaned_data.get("secret")
        if secret:
            instance.set_secret(secret)
        if commit:
            instance.save()
        return instance


@admin.register(Mailbox)
class MailboxAdmin(admin.ModelAdmin):
    form = MailboxAdminForm
    list_display = (
        "name",
        "email",
        "bound_group",
        "is_fallback",
        "is_active",
        "last_uid",
        "uidvalidity",
        "created_at",
    )
    list_filter = ("is_fallback", "is_active", "imap_ssl", "smtp_ssl")
    search_fields = ("name", "email", "username", "imap_host", "smtp_host")
    readonly_fields = ("last_uid", "uidvalidity", "created_at", "secret_status")

    @admin.display(description="绑定组")
    def bound_group(self, obj):
        group = obj.owning_group
        return group.name if group else "—（统一进线/兜底）"

    @admin.display(description="凭据")
    def secret_status(self, obj):
        return "已配置（密文存储）" if obj.secret_encrypted else "未配置"


class UserGroupInline(admin.TabularInline):
    model = UserGroup
    extra = 1
    autocomplete_fields = ("group",)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        "username",
        "first_name",
        "email",
        "is_superadmin",
        "is_staff",
        "is_active",
    )
    list_filter = ("is_superadmin", "is_staff", "is_active", "user_groups__group")
    search_fields = ("username", "first_name", "last_name", "email")
    inlines = [UserGroupInline]
    fieldsets = BaseUserAdmin.fieldsets + (
        ("工单系统", {"fields": ("is_superadmin",)}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("工单系统", {"fields": ("is_superadmin",)}),
    )



@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ("name", "mailbox", "identity_email_display", "is_admin_group", "member_count", "created_at")
    list_filter = ("is_admin_group",)
    search_fields = ("name", "mailbox__email")
    autocomplete_fields = ("mailbox",)

    @admin.display(description="对外身份邮箱")
    def identity_email_display(self, obj):
        return obj.identity_email or "—（无，且未配置全局兜底）"

    @admin.display(description="成员数")
    def member_count(self, obj):
        return obj.user_groups.count()


@admin.register(UserGroup)
class UserGroupAdmin(admin.ModelAdmin):
    list_display = ("user", "group", "is_admin", "created_at")
    list_filter = ("is_admin", "group")
    search_fields = ("user__username", "group__name")
    autocomplete_fields = ("user", "group")
