"""账号与邮箱管理表单（开发文档 §2.1 / §2.7 / §10.3）。

要点：
- 用户 ↔ 组两个方向的成员关系共用同一套同步工具（`update_or_create` + `delete`），
  既不会产生重复成员关系（`unique_user_group` 约束），也能正确清理取消选中的关系。
- 邮箱凭据只以 Fernet 密文落库：表单用 `PasswordInput` 且不回显，留空表示不修改，
  新建时必须填写（`Mailbox.secret_encrypted` 非空）。
- 「最后一个超级管理员」的应用层不变量：不允许降级或停用最后一名启用状态的超管。
"""

from __future__ import annotations

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q, QuerySet

from apps.accounts.models import Group, Mailbox, User, UserGroup
from apps.accounts.provider_presets import PRESETS_BY_KEY, PROVIDER_CHOICES

USER_ADMIN_PREFIX = "membership_admin_"
GROUP_ADMIN_PREFIX = "member_admin_"


# ------------------------------------------------------------------ 共享工具
def mailbox_choices(group: Group | None = None) -> QuerySet[Mailbox]:
    """可绑定到组的邮箱：未被其他组占用 + 当前组已绑定的邮箱（§2.1）。"""
    queryset = Mailbox.objects.filter(group__isnull=True)
    if group is not None and group.pk and group.mailbox_id:
        queryset = Mailbox.objects.filter(Q(group__isnull=True) | Q(pk=group.mailbox_id))
    return queryset.order_by("name", "id")


def _sync_memberships(
    *,
    user: User | None = None,
    group: Group | None = None,
    counterpart_ids: set[int],
    admin_ids: set[int],
) -> None:
    """同步「一侧」的组成员关系（用户管理/组管理共用）。

    - 传 `user` 时，`counterpart_ids` 为组 ID 集合（用户视角）；
    - 传 `group` 时，`counterpart_ids` 为用户 ID 集合（组视角）。

    先删除不再选中的关系，再对选中的关系 `update_or_create`，
    因此不会产生重复行，也不会误删另一侧的成员关系。
    """
    if user is not None:
        existing = UserGroup.objects.filter(user=user)
        lookup: dict = {"user": user}
        key = "group_id"
    else:
        existing = UserGroup.objects.filter(group=group)
        lookup = {"group": group}
        key = "user_id"

    existing.exclude(**{f"{key}__in": counterpart_ids}).delete()
    for counterpart_id in sorted(counterpart_ids):
        UserGroup.objects.update_or_create(
            defaults={"is_admin": counterpart_id in admin_ids},
            **lookup,
            **{key: counterpart_id},
        )


def sync_user_groups(user: User, groups, admin_group_ids=()) -> None:
    """把用户所属组同步为 `groups`，管理员身份取 `admin_group_ids`（用户管理）。"""
    group_ids = {group.pk for group in groups}
    admin_ids = {int(pk) for pk in admin_group_ids} & group_ids
    _sync_memberships(user=user, counterpart_ids=group_ids, admin_ids=admin_ids)


def sync_group_members(group: Group, users, admin_user_ids=()) -> None:
    """把组成员同步为 `users`，组内管理员取 `admin_user_ids`（组管理）。"""
    user_ids = {user.pk for user in users}
    admin_ids = {int(pk) for pk in admin_user_ids} & user_ids
    _sync_memberships(group=group, counterpart_ids=user_ids, admin_ids=admin_ids)


def _checked_ids(cleaned_data: dict, prefix: str, candidates) -> set[int]:
    """从 cleaned_data 里收集所有被勾选的 `<prefix><id>` 复选框。"""
    ids: set[int] = set()
    for candidate in candidates:
        if cleaned_data.get(f"{prefix}{candidate.pk}"):
            ids.add(candidate.pk)
    return ids


# ------------------------------------------------------------------ 用户管理
class UserForm(forms.ModelForm):
    """用户新建/编辑：账号字段 + 所属组 + 组内管理员身份 + 密码。

    - 新建时密码必填，编辑时留空表示不修改；
    - 密码一律走 `user.set_password()`（Argon2），绝不明文落库。
    """

    password = forms.CharField(
        label="密码",
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="新建用户必填；编辑时留空表示不修改现有密码。",
    )
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="所属组",
    )

    class Meta:
        model = User
        fields = [
            "username",
            "first_name",
            "last_name",
            "email",
            "is_active",
            "is_superadmin",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.group_queryset = list(Group.objects.all().order_by("name"))
        self.fields["groups"].queryset = Group.objects.all().order_by("name")
        for group in self.group_queryset:
            self.fields[f"{USER_ADMIN_PREFIX}{group.pk}"] = forms.BooleanField(
                required=False,
                label=f"{group.name} 组内管理员",
            )
        if self.instance.pk:
            self.fields["groups"].initial = list(self.instance.user_groups.values_list("group_id", flat=True))
            for membership in self.instance.user_groups.all():
                field = self.fields.get(f"{USER_ADMIN_PREFIX}{membership.group_id}")
                if field is not None:
                    field.initial = membership.is_admin

    # ---- 模板辅助 ----
    def admin_fields(self) -> list[dict]:
        """按组渲染「组内管理员」复选框：模板里用 `{% for row in form.admin_fields %}`。"""
        return [
            {"group": group, "field": self[f"{USER_ADMIN_PREFIX}{group.pk}"]} for group in self.group_queryset
        ]

    def admin_group_ids(self) -> set[int]:
        return _checked_ids(self.cleaned_data, USER_ADMIN_PREFIX, self.group_queryset)

    # ---- 校验 ----
    def clean_password(self) -> str:
        password = self.cleaned_data.get("password") or ""
        if not self.instance.pk and not password:
            raise forms.ValidationError("新建用户必须设置密码。")
        if password:
            # 接入 settings.AUTH_PASSWORD_VALIDATORS（长度 ≥10 等），
            # 与 Django 的 UserCreationForm 行为一致。
            candidate = self.instance if self.instance.pk else User(
                username=self.cleaned_data.get("username", ""),
                email=self.cleaned_data.get("email", ""),
                first_name=self.cleaned_data.get("first_name", ""),
                last_name=self.cleaned_data.get("last_name", ""),
            )
            try:
                validate_password(password, user=candidate)
            except DjangoValidationError as exc:
                raise forms.ValidationError(list(exc.messages)) from exc
        return password

    def clean(self):
        cleaned = super().clean()
        is_superadmin = cleaned.get("is_superadmin")
        is_active = cleaned.get("is_active")
        if self.instance.pk:
            was_active_superadmin = User.objects.filter(
                pk=self.instance.pk, is_superadmin=True, is_active=True
            ).exists()
            if was_active_superadmin and not (is_superadmin and is_active):
                has_other = (
                    User.objects.filter(is_superadmin=True, is_active=True)
                    .exclude(pk=self.instance.pk)
                    .exists()
                )
                if not has_other:
                    raise forms.ValidationError(
                        "不能降级或停用最后一个超级管理员：系统必须保留至少一名启用状态的超级管理员。"
                    )
        return cleaned

    # ---- 保存 ----
    def save(self, commit: bool = True):
        user = super().save(commit=False)
        password = self.cleaned_data.get("password")
        if password:
            user.set_password(password)
        # 应用层超级管理员同时具备 Django Admin 权限（与 seed_demo 的账号一致）；
        # 取消超管时同步收回，避免出现"已降级但仍能进 /admin/"的残留权限。
        user.is_staff = bool(user.is_superadmin)
        user.is_superuser = bool(user.is_superadmin)
        if commit:
            user.save()
            sync_user_groups(user, self.cleaned_data.get("groups") or [], self.admin_group_ids())
        return user


# ------------------------------------------------------------------ 用户组管理
class GroupForm(forms.ModelForm):
    """用户组新建/编辑：组信息 + 成员 + 组内管理员身份。"""

    mailbox = forms.ModelChoiceField(
        queryset=Mailbox.objects.none(),
        required=False,
        label="对外邮箱",
        empty_label="— 无邮箱（回信走全局兜底邮箱）—",
        help_text="留空表示该组只做权限容器，对外回信走全局兜底邮箱（§2.1）。",
    )
    members = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="成员",
    )

    class Meta:
        model = Group
        fields = ["name", "mailbox", "is_admin_group"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["mailbox"].queryset = mailbox_choices(self.instance)
        self.user_queryset = list(User.objects.all().order_by("username"))
        self.fields["members"].queryset = User.objects.all().order_by("username")
        for user in self.user_queryset:
            self.fields[f"{GROUP_ADMIN_PREFIX}{user.pk}"] = forms.BooleanField(
                required=False,
                label=f"{user.display_name} 组内管理员",
            )
        if self.instance.pk:
            self.fields["members"].initial = list(self.instance.user_groups.values_list("user_id", flat=True))
            for membership in self.instance.user_groups.all():
                field = self.fields.get(f"{GROUP_ADMIN_PREFIX}{membership.user_id}")
                if field is not None:
                    field.initial = membership.is_admin

    # ---- 模板辅助 ----
    def admin_fields(self) -> list[dict]:
        """按用户渲染「组内管理员」复选框。"""
        return [
            {"user": user, "field": self[f"{GROUP_ADMIN_PREFIX}{user.pk}"]} for user in self.user_queryset
        ]

    def admin_user_ids(self) -> set[int]:
        return _checked_ids(self.cleaned_data, GROUP_ADMIN_PREFIX, self.user_queryset)

    # ---- 保存 ----
    def save(self, commit: bool = True):
        group = super().save(commit=commit)
        if commit:
            sync_group_members(group, self.cleaned_data.get("members") or [], self.admin_user_ids())
        return group


# ------------------------------------------------------------------ 邮箱配置
class MailboxForm(forms.ModelForm):
    """邮箱配置：字段与 MailboxAdminForm 一致；凭据只写不读。

    `provider` 是**非模型字段**，只用于"选服务商 → 自动填主机/端口/加密"的便利：
    它不会被写入数据库，也不会覆盖管理员手工填写的值（仅当对应主机为空时才做兜底填充）。
    系统本身只依赖标准 IMAP/SMTP，不绑定任何服务商。
    """

    provider = forms.ChoiceField(
        label="服务商预设",
        required=False,
        choices=PROVIDER_CHOICES,
        help_text="选择后会自动填入主机/端口/加密方式（页面脚本即时填充；未启用脚本时保存阶段也会兜底填充）。",
    )

    secret = forms.CharField(
        label="授权码 / 密码",
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="留空表示不修改已保存的凭据；保存后仅以 Fernet 密文存储，页面不再回显。",
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
        help_texts = {
            "is_fallback": "全局兜底邮箱，全局唯一：无邮箱组的对外回信都由它发出（§2.1）。",
            "imap_host": "IMAP 服务器地址；选择服务商预设可留空，保存时自动填充。",
            "smtp_host": "SMTP 服务器地址；选择服务商预设可留空，保存时自动填充。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 允许"只选服务商预设、主机留空"提交：真正的非空校验放在 clean() 里，
        # 这样既不放松模型约束（数据库仍要求非空），又能让无脚本环境正常保存。
        self.fields["imap_host"].required = False
        self.fields["smtp_host"].required = False
        # 端口留空时：优先用所选预设的端口，没有预设则用模型默认值（993 / 465）
        self.fields["imap_port"].required = False
        self.fields["smtp_port"].required = False

    def clean(self):
        cleaned = super().clean()
        self._apply_provider_preset(cleaned)
        if not self.instance.pk and not cleaned.get("secret"):
            self.add_error("secret", "新建邮箱必须填写授权码/密码。")

        if cleaned.get("is_fallback"):
            others = Mailbox.objects.filter(is_fallback=True)
            if self.instance.pk:
                others = others.exclude(pk=self.instance.pk)
            if others.exists():
                self.add_error(
                    "is_fallback",
                    "已存在全局兜底邮箱，全局只能有一个（开发文档 §2.1）。",
                )
        return cleaned

    def _apply_provider_preset(self, cleaned: dict) -> None:
        """服务端兜底：主机为空时用所选预设补齐（不覆盖已填写的值）。

        这样即使浏览器没有执行静态脚本，管理员选择服务商后也能保存出可用的连接参数。
        """
        preset = PRESETS_BY_KEY.get((cleaned.get("provider") or "").strip())
        if preset:
            if not (cleaned.get("imap_host") or "").strip():
                cleaned["imap_host"] = preset["imap_host"]
                self.instance.imap_host = preset["imap_host"]
            if not (cleaned.get("smtp_host") or "").strip():
                cleaned["smtp_host"] = preset["smtp_host"]
                self.instance.smtp_host = preset["smtp_host"]
            if not cleaned.get("imap_port"):
                cleaned["imap_port"] = preset["imap_port"]
                self.instance.imap_port = preset["imap_port"]
            if not cleaned.get("smtp_port"):
                cleaned["smtp_port"] = preset["smtp_port"]
                self.instance.smtp_port = preset["smtp_port"]

        # 补齐之后再校验非空（模型层仍是非空字段，不放松不变量）
        if not (cleaned.get("imap_host") or "").strip():
            self.add_error("imap_host", "请填写 IMAP 主机，或选择一个服务商预设。")
        if not (cleaned.get("smtp_host") or "").strip():
            self.add_error("smtp_host", "请填写 SMTP 主机，或选择一个服务商预设。")
        for field in ("imap_port", "smtp_port"):
            value = cleaned.get(field)
            if value is not None and not (1 <= int(value) <= 65535):
                self.add_error(field, "端口需在 1–65535 之间。")

    def save(self, commit: bool = True):
        instance = super().save(commit=False)
        secret = self.cleaned_data.get("secret")
        if secret:
            instance.set_secret(secret)
        if commit:
            instance.save()
        return instance
