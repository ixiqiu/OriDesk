"""路由规则与系统设置表单（开发文档 §4.4 / §4.6 / §5.2）。

设计要点：
- `RuleForm` 是 ModelForm，字段与 §4.4 完全一致；额外提供 `action_group` 下拉，
  让 `action_type=assign_group` 时不必手填组 ID（保存时写入 `action_value`）。
- `SystemSettingsForm` 每个字段对应 §4.6 的一个配置项，统一经 `Setting.set(...)`
  写入，从而自动产生审计记录（谁在何时改了哪一项，§2.5）。
"""

from __future__ import annotations

import re
from email.message import EmailMessage

from django import forms

from apps.accounts.models import Group, Mailbox, User
from apps.audit.models import Setting
from apps.core.utils import extract_email
from apps.routing.models import Rule
from apps.tickets.models import Tag, Ticket

# ---------------------------------------------------------------------- 规则


class RuleForm(forms.ModelForm):
    """新建 / 编辑路由规则。"""

    action_group = forms.ModelChoiceField(
        queryset=Group.objects.none(),
        required=False,
        label="动作值：用户组",
        empty_label="— 请选择用户组 —",
        help_text="action_type=分配到组 时使用；选择后自动写入动作值，无需手填组 ID。",
    )

    class Meta:
        model = Rule
        fields = [
            "mailbox",
            "priority",
            "enabled",
            "match_field",
            "match_op",
            "match_value",
            "action_type",
            "action_value",
        ]
        labels = {
            "mailbox": "入口邮箱",
            "priority": "优先级",
            "enabled": "启用",
            "match_field": "匹配字段",
            "match_op": "匹配方式",
            "match_value": "匹配值",
            "action_type": "动作类型",
            "action_value": "动作值",
        }
        help_texts = {
            "priority": "数字越小越优先；命中第一条规则即停止匹配。",
            "match_value": "contains：子串；equals：完全相等；domain：发件人域名；regex：正则表达式。",
            "action_value": "assign_group：组 ID 或组名；assign_user：用户名或用户 ID；"
            "add_tag：标签名；set_status：open / pending / closed。",
        }
        widgets = {
            "match_value": forms.TextInput(attrs={"placeholder": "例如：发票"}),
            "action_value": forms.TextInput(attrs={"placeholder": "选择用户组后自动填充"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["action_group"].queryset = Group.objects.all().order_by("name")
        self.fields["mailbox"].queryset = Mailbox.objects.all().order_by("id")
        # action_value 可由 action_group 下拉推导，必填性在 clean() 中按 action_type 判断。
        self.fields["action_value"].required = False
        instance = self.instance
        if not self.is_bound and instance and instance.pk and instance.action_type == "assign_group":
            value = (instance.action_value or "").strip()
            if value.isdigit() and Group.objects.filter(pk=int(value)).exists():
                self.fields["action_group"].initial = int(value)

    def clean(self):
        cleaned = super().clean()
        action_type = cleaned.get("action_type")
        value = (cleaned.get("action_value") or "").strip()
        group = cleaned.get("action_group")

        if action_type == "assign_group":
            if group is not None:
                cleaned["action_value"] = str(group.pk)
            elif not value:
                self.add_error("action_group", "请选择要分配的用户组。")
        elif action_type == "assign_user":
            if not value:
                self.add_error("action_value", "请填写用户名或用户 ID。")
            elif not self._user_exists(value):
                self.add_error("action_value", "未找到该用户，请核对用户名或用户 ID。")
        elif action_type == "set_status":
            if value not in dict(Ticket.STATUS_CHOICES):
                self.add_error("action_value", "状态值必须是 open / pending / closed 之一。")
        elif action_type == "add_tag" and not value:
            self.add_error("action_value", "请填写标签名。")

        if cleaned.get("match_op") == "regex":
            raw = (cleaned.get("match_value") or "").strip()
            if raw:
                try:
                    re.compile(raw)
                except re.error as exc:
                    self.add_error("match_value", f"正则表达式非法：{exc}")
        return cleaned

    @staticmethod
    def _user_exists(value: str) -> bool:
        if value.isdigit() and User.objects.filter(pk=int(value)).exists():
            return True
        return User.objects.filter(username=value).exists()


# ---------------------------------------------------------------------- 规则试算


class RuleTrialForm(forms.Form):
    """规则试算小工具：构造一封虚拟邮件，只看命中结果，不写库。"""

    mailbox = forms.ModelChoiceField(
        queryset=Mailbox.objects.none(),
        label="入口邮箱",
        empty_label="— 请选择入口邮箱 —",
        help_text="规则只对指定入口邮箱生效，因此试算必须选择邮箱。",
    )
    subject = forms.CharField(required=False, label="主题")
    sender = forms.CharField(required=False, label="发件人", widget=forms.TextInput(attrs={"placeholder": "customer@example.com"}))
    to = forms.CharField(required=False, label="收件人", widget=forms.TextInput(attrs={"placeholder": "support@example.com"}))
    body = forms.CharField(
        required=False,
        label="正文",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "邮件纯文本正文"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["mailbox"].queryset = Mailbox.objects.all().order_by("id")

    def clean(self):
        cleaned = super().clean()
        if not any(
            (cleaned.get(name) or "").strip()
            for name in ("subject", "sender", "to", "body")
        ):
            raise forms.ValidationError("请至少填写主题 / 发件人 / 收件人 / 正文中的一项。")
        return cleaned

    def build_message(self) -> EmailMessage:
        """用 email.message.EmailMessage 构造一封与真实入站同构的邮件。"""
        msg = EmailMessage()
        subject = (self.cleaned_data.get("subject") or "").strip()
        sender = (self.cleaned_data.get("sender") or "").strip()
        to = (self.cleaned_data.get("to") or "").strip()
        body = (self.cleaned_data.get("body") or "").strip()
        if subject:
            msg["Subject"] = subject
        if sender:
            msg["From"] = sender
        if to:
            msg["To"] = to
        msg.set_content(body or "")
        return msg

    def sender_address(self) -> str:
        """归一化发件人地址，用于粘性判断（与 pipeline 保持一致）。"""
        return extract_email(self.cleaned_data.get("sender") or "")


# ---------------------------------------------------------------------- 系统设置


class SystemSettingsForm(forms.Form):
    """开发文档 §4.6 的六项关键配置。"""

    TEXT_KEYS = (
        "sticky_window_days",
        "first_contact_window_hours",
        "max_attachment_size_mb",
        "imap_poll_interval_seconds",
    )
    OBJECT_KEYS = ("fallback_group_id", "fallback_mailbox_id")

    sticky_window_days = forms.IntegerField(
        min_value=1,
        max_value=365,
        label="短期粘性天数（sticky_window_days）",
        help_text="默认 7 天，取值范围 1-365。同发件人在该天数内再次进线，归原组。",
    )
    first_contact_window_hours = forms.IntegerField(
        min_value=1,
        max_value=8760,
        label="首次进线判断窗口（first_contact_window_hours）",
        help_text="默认 24 小时，正整数（最大 8760 = 365 天）。同发件人在该窗口内无新建工单才发自动回复。",
    )
    fallback_group_id = forms.ModelChoiceField(
        queryset=Group.objects.none(),
        required=False,
        label="兜底组（fallback_group_id）",
        empty_label="— 不设置兜底组 —",
        help_text="默认未配置（空）。未命中任何规则时，优先进入该组。",
    )
    fallback_mailbox_id = forms.ModelChoiceField(
        queryset=Mailbox.objects.none(),
        required=False,
        label="全局兜底邮箱（fallback_mailbox_id）",
        empty_label="— 不配置兜底邮箱 —",
        help_text="默认未配置（空）。无邮箱组对外发信时使用该邮箱。",
    )
    max_attachment_size_mb = forms.IntegerField(
        min_value=1,
        max_value=100,
        label="附件大小上限（max_attachment_size_mb）",
        help_text="默认 25 MB，取值范围 1-100。",
    )
    imap_poll_interval_seconds = forms.IntegerField(
        min_value=10,
        max_value=86400,
        label="IMAP 轮询间隔（imap_poll_interval_seconds）",
        help_text="默认 60 秒，最小 10 秒。",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fallback_group_id"].queryset = Group.objects.all().order_by("name")
        self.fields["fallback_mailbox_id"].queryset = Mailbox.objects.all().order_by("id")
        if not self.is_bound:
            self.initial.update(self.current_values())

    @classmethod
    def current_values(cls) -> dict:
        """从 Setting 读取当前值作为初始值。"""
        values: dict = {}
        for key in cls.TEXT_KEYS:
            raw = Setting.get(key)
            if raw not in (None, ""):
                values[key] = raw
        for key in cls.OBJECT_KEYS:
            raw = Setting.get_optional_int(key)
            if raw:
                values[key] = raw
        return values

    def save(self, *, user=None) -> list[str]:
        """逐项写入 Setting（经 Setting.set 写审计），返回已写入的键。"""
        written: list[str] = []
        for key in self.TEXT_KEYS:
            value = self.cleaned_data.get(key)
            Setting.set(key, "" if value is None else value, user=user)
            written.append(key)
        for key in self.OBJECT_KEYS:
            obj = self.cleaned_data.get(key)
            Setting.set(key, obj.pk if obj else "", user=user)
            written.append(key)
        return written


# ---------------------------------------------------------------------- 标签字典（v1.2）


class TagForm(forms.ModelForm):
    """标签字典的新建 / 编辑（仅管理员可用，由视图的 @routing_manager_required 保证）。

    「组作用域内同名唯一」**不在本表单里重写**：ModelForm 的 `_post_clean()` 会用表单数据
    构造 Tag 实例并调用 `full_clean()`，进而触发 `Tag.clean()` 的唯一性校验，
    `ValidationError({"name": ...})` 会被自动转成表单字段错误（§10.1 模型先行）。
    """

    class Meta:
        model = Tag
        fields = ["name", "group", "color", "description", "is_active"]
        labels = {
            "name": "标签名",
            "group": "作用域",
            "color": "角标颜色",
            "description": "说明",
            "is_active": "启用",
        }
        help_texts = {
            "name": "同一作用域内不能重名（不区分大小写）；首尾空白会被自动去掉。",
            "group": "留空 = 全局标签（所有组可用）；选择用户组 = 该组专属标签。",
            "color": "仅影响界面角标配色，不影响业务逻辑。",
            "description": "可选，鼠标悬停角标时显示。",
            "is_active": "停用后不能再被打到工单上，但已打的标签会保留。",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "例如：紧急", "autocomplete": "off"}),
            "description": forms.TextInput(attrs={"placeholder": "可选"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].queryset = Group.objects.all().order_by("name")
        self.fields["group"].empty_label = "— 全局标签（所有组可用）—"
