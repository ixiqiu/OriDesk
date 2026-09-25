"""工单界面表单。

安全要点（§10.3）：
- 所有富文本在入库前经 sanitize_html 净化（在视图中调用）。
- 附件数量与大小在此校验，超限直接报错，不进入发信流程。
"""

from __future__ import annotations

from django import forms

from apps.accounts.models import Group, User
from apps.mailboxes.storage import max_attachment_bytes
from apps.tickets.models import Ticket

MAX_ATTACHMENT_COUNT = 10


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"multiple": True}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(item, initial) for item in data]
        return [single(data, initial)] if data else []


class ReplyForm(forms.Form):
    """回复表单：富文本正文 + 可选附件。"""

    body_html = forms.CharField(required=False, widget=forms.HiddenInput())
    body_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 8, "placeholder": "在此输入回复内容…"}),
    )
    cc = forms.CharField(
        required=False,
        label="抄送",
        widget=forms.TextInput(attrs={"placeholder": "多个地址用逗号分隔，可留空"}),
    )
    attachments = MultipleFileField(required=False, label="附件")

    def clean(self):
        cleaned = super().clean()
        body_html = (cleaned.get("body_html") or "").strip()
        body_text = (cleaned.get("body_text") or "").strip()
        if not body_html and not body_text:
            raise forms.ValidationError("回复内容不能为空。")
        files = cleaned.get("attachments") or []
        if len(files) > MAX_ATTACHMENT_COUNT:
            raise forms.ValidationError(f"一次最多上传 {MAX_ATTACHMENT_COUNT} 个附件。")
        limit = max_attachment_bytes()
        for item in files:
            if item.size > limit:
                raise forms.ValidationError(
                    f"附件 {item.name} 超过大小上限 {limit // (1024 * 1024)} MB。"
                )
        return cleaned

    def cc_list(self) -> list[str]:
        raw = self.cleaned_data.get("cc") or ""
        return [addr.strip() for addr in raw.split(",") if addr.strip()]


class NoteForm(forms.Form):
    """内部备注：组内可见，不发给客户。"""

    body_text = forms.CharField(
        label="内部备注",
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "仅组内可见，客户不会收到这封内容"}),
    )


class ReassignForm(forms.Form):
    """改派到任意组（§2.2）。"""

    group = forms.ModelChoiceField(queryset=Group.objects.none(), label="改派到")
    reason = forms.CharField(required=False, label="原因", max_length=200)

    def __init__(self, *args, ticket: Ticket | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Group.objects.all().order_by("name")
        if ticket is not None:
            queryset = queryset.exclude(pk=ticket.group_id)
        self.fields["group"].queryset = queryset


class StatusForm(forms.Form):
    status = forms.ChoiceField(choices=Ticket.STATUS_CHOICES, label="状态")


class AssignForm(forms.Form):
    """把工单指派给组内成员（认领的一种：代认领）。"""

    assignee = forms.ModelChoiceField(
        queryset=User.objects.none(), required=False, label="指派给", empty_label="— 不指派 —"
    )

    def __init__(self, *args, ticket: Ticket | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if ticket is not None:
            self.fields["assignee"].queryset = User.objects.filter(
                user_groups__group=ticket.group
            ).distinct().order_by("username")


class InboxFilterForm(forms.Form):
    """收件箱筛选器（GET 形式，无需 CSRF）。"""

    q = forms.CharField(required=False, label="搜索")
    group = forms.ModelChoiceField(queryset=Group.objects.all(), required=False, label="用户组")
    status = forms.ChoiceField(
        choices=[("", "全部状态")] + Ticket.STATUS_CHOICES, required=False, label="状态"
    )
    awaiting = forms.ChoiceField(
        choices=[("", "全部"), ("1", "仅待回复"), ("0", "仅非待回复")],
        required=False,
        label="待回复",
    )
    assignee = forms.CharField(required=False, label="认领人")
