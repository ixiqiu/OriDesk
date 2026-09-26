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
from apps.notifications import ntfy
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
    """开发文档 §4.6 的六项关键配置 + 移动端推送配置（契约 §6.1）。

    ## 写入语义（重要，别改成"全量覆盖"）
    `save()` **只写表单实际提交过的键**。三条理由：

    1. **令牌不能被静默清掉**。`ntfy_token` 是密码框，浏览器回显不了原值，
       若按"空即清空"处理，管理员每次保存别的设置都会顺手把令牌清掉 ——
       推送随即全线失效，且现象是"前几天还好好的"。
    2. **兼容既有调用方**。已有测试与脚本只提交那六个键，不应因为新增了
       推送配置就顺手把它们重置成默认值。
    3. **少写无谓审计**。表单没提交的项本来就没变，不该产生 `config_change` 记录。
    """

    TEXT_KEYS = (
        "sticky_window_days",
        "first_contact_window_hours",
        "max_attachment_size_mb",
        "imap_poll_interval_seconds",
    )
    OBJECT_KEYS = ("fallback_group_id", "fallback_mailbox_id")

    # 移动端推送（apps/notifications，契约 §6.1）
    PUSH_TEXT_KEYS = ("ntfy_server_url", "ntfy_topic_prefix", "mobile_public_base_url")
    TOKEN_KEY = "ntfy_token"

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

    # ---------------------------------------------------------- 移动端推送
    ntfy_enabled = forms.BooleanField(
        required=False,
        label="启用移动端推送（ntfy_enabled）",
        help_text="默认关闭。关闭时不会向任何设备发布，即使下面填了地址。",
    )
    ntfy_server_url = forms.CharField(
        required=False,
        label="ntfy 服务地址（ntfy_server_url）",
        widget=forms.TextInput(attrs={"placeholder": "https://ntfy.example.com"}),
        help_text=(
            "必须是手机能访问到的 https 地址，例如 https://ntfy.example.com。"
            "不要填 127.0.0.1 或容器内网名 —— 那样后端能发出去、手机却订阅不到。"
        ),
    )
    ntfy_token = forms.CharField(
        required=False,
        label="ntfy 访问令牌（ntfy_token）",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="留空 = 保持已保存的令牌不变。令牌以 Fernet 密文入库，界面永不回显明文。",
    )
    ntfy_token_clear = forms.BooleanField(
        required=False,
        label="清除已保存的 ntfy 令牌",
        help_text="实例未开鉴权时用不上令牌，可勾选清除。",
    )
    ntfy_topic_prefix = forms.CharField(
        required=False,
        max_length=16,
        label="topic 前缀（ntfy_topic_prefix）",
        help_text="默认 oridesk。只影响新建订阅的 topic 命名，便于在 ntfy 侧辨认。",
    )
    notify_aggregate_seconds = forms.IntegerField(
        required=False,
        min_value=5,
        max_value=3600,
        label="通知聚合窗口秒数（notify_aggregate_seconds）",
        help_text="默认 60 秒。窗口内同一批事件合并为一条通知，避免一次收信刷屏。",
    )
    mobile_public_base_url = forms.CharField(
        required=False,
        label="对外地址（mobile_public_base_url）",
        widget=forms.TextInput(attrs={"placeholder": "https://desk.example.com"}),
        help_text="推送通知里「在浏览器打开」的兜底链接。留空则通知只唤起 App，不给网页兜底。",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fallback_group_id"].queryset = Group.objects.all().order_by("name")
        self.fields["fallback_mailbox_id"].queryset = Mailbox.objects.all().order_by("id")
        if not self.is_bound:
            self.initial.update(self.current_values())

    @classmethod
    def current_values(cls) -> dict:
        """从 Setting 读取当前值作为初始值。

        **刻意不含 `ntfy_token`**：令牌不回显（哪怕是密文也不该出现在页面源码里）。
        页面只显示"是否已配置"，见视图传入的 `ntfy_token_set`。
        """
        values: dict = {}
        for key in cls.TEXT_KEYS:
            raw = Setting.get(key)
            if raw not in (None, ""):
                values[key] = raw
        for key in cls.OBJECT_KEYS:
            raw = Setting.get_optional_int(key)
            if raw:
                values[key] = raw
        for key in cls.PUSH_TEXT_KEYS:
            raw = Setting.get(key)
            if raw not in (None, ""):
                values[key] = raw
        if Setting.get_bool("ntfy_enabled", False):
            values["ntfy_enabled"] = True
        aggregate = Setting.get_int("notify_aggregate_seconds", 60)
        if aggregate:
            values["notify_aggregate_seconds"] = aggregate
        return values

    @staticmethod
    def _clean_public_url(value: str, field_label: str) -> str:
        """校验对外地址：非空时必须以 http(s):// 开头，并去掉尾斜杠。"""
        text = (value or "").strip().rstrip("/")
        if not text:
            return ""
        if not text.lower().startswith(("http://", "https://")):
            raise forms.ValidationError(f"{field_label}必须以 http:// 或 https:// 开头。")
        return text

    def clean_ntfy_server_url(self) -> str:
        value = self._clean_public_url(
            self.cleaned_data.get("ntfy_server_url"), "ntfy 服务地址"
        )
        if value.lower().startswith("http://"):
            # **硬拒绝**而不是给个警告：App 侧 usesCleartextTraffic=false，
            # 明文 HTTP 的 ntfy 地址手机根本连不上。放行只会制造
            # "后端说发送成功、手机什么也收不到"这种最难查的故障。
            raise forms.ValidationError(
                "必须是 https:// 地址。客户端强制 HTTPS（usesCleartextTraffic=false），"
                "明文 HTTP 的 ntfy 手机连不上，表现为「发送成功但收不到」。"
            )
        return value

    def clean_mobile_public_base_url(self) -> str:
        return self._clean_public_url(
            self.cleaned_data.get("mobile_public_base_url"), "对外地址"
        )

    def clean_ntfy_topic_prefix(self) -> str:
        """前缀会被拼进 topic，必须落在 ntfy 允许的字符集内（[-_A-Za-z0-9]）。"""
        raw = (self.cleaned_data.get("ntfy_topic_prefix") or "").strip()
        if not raw:
            return ""
        cleaned = re.sub(r"[^\-_A-Za-z0-9]", "", raw)
        if cleaned != raw:
            raise forms.ValidationError(
                "只能包含字母、数字、下划线与连字符（ntfy 的 topic 字符集限制）。"
            )
        return cleaned

    def save(self, *, user=None) -> list[str]:
        """逐项写入 Setting（经 Setting.set 写审计），返回已写入的键。"""
        written: list[str] = []

        for key in self.TEXT_KEYS:
            if key not in self.data:
                continue
            value = self.cleaned_data.get(key)
            Setting.set(key, "" if value is None else value, user=user)
            written.append(key)
        for key in self.OBJECT_KEYS:
            if key not in self.data:
                continue
            obj = self.cleaned_data.get(key)
            Setting.set(key, obj.pk if obj else "", user=user)
            written.append(key)

        # ---- 移动端推送 ----
        if "ntfy_enabled" in self.data:
            Setting.set(
                "ntfy_enabled",
                "true" if self.cleaned_data.get("ntfy_enabled") else "false",
                user=user,
            )
            written.append("ntfy_enabled")
        for key in self.PUSH_TEXT_KEYS:
            if key not in self.data:
                continue
            Setting.set(key, self.cleaned_data.get(key) or "", user=user)
            written.append(key)
        if "notify_aggregate_seconds" in self.data:
            value = self.cleaned_data.get("notify_aggregate_seconds")
            Setting.set("notify_aggregate_seconds", 60 if value is None else value, user=user)
            written.append("notify_aggregate_seconds")

        # 令牌单独处理：走 ntfy.set_token 做 Fernet 加密（决策 D4），
        # **不能**直接 Setting.set 明文。清空必须显式勾选"清除"，见类文档。
        if self.cleaned_data.get("ntfy_token_clear"):
            ntfy.set_token("", user=user)
            written.append(self.TOKEN_KEY)
        elif (self.cleaned_data.get(self.TOKEN_KEY) or "").strip():
            ntfy.set_token(self.cleaned_data[self.TOKEN_KEY].strip(), user=user)
            written.append(self.TOKEN_KEY)

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
