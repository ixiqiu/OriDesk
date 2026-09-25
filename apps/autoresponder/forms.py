"""自动回复模板表单（开发文档 §2.4 / §5.3）。

模板正文用纯文本编辑（发信正文为 `body_text`），变量语法为 Django 模板
`{{ ticket_id }}`，可用变量见 `apps.autoresponder.services.TEMPLATE_VARIABLES`。
"""

from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from apps.autoresponder.services import TEMPLATE_VARIABLES


class TemplateForm(forms.Form):
    body = forms.CharField(
        label="模板正文",
        widget=forms.Textarea(attrs={"rows": 18, "class": "mono", "spellcheck": "false"}),
        help_text=(
            "支持变量："
            + "、".join("{{ %s }}" % name for name in TEMPLATE_VARIABLES)
            + "。留空会导致自动回复内容为空，因此不允许。"
        ),
    )

    def clean_body(self) -> str:
        body = self.cleaned_data["body"]
        if not body.strip():
            raise ValidationError("模板正文不能为空。")
        return body
