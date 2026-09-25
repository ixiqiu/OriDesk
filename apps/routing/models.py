"""apps.routing：规则与模板（开发文档 §4.4）。"""

from __future__ import annotations

import logging
import re

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.utils import (
    decode_mime_header,
    extract_email,
    extract_emails,
    plain_text_from_message,
)

logger = logging.getLogger(__name__)


class Rule(models.Model):
    MATCH_FIELD_CHOICES = [
        ("subject", "主题"),
        ("from", "发件人"),
        ("to", "收件人"),
        ("body", "正文"),
    ]
    MATCH_OP_CHOICES = [
        ("contains", "包含"),
        ("regex", "正则"),
        ("equals", "等于"),
        ("domain", "域名"),
    ]
    ACTION_TYPE_CHOICES = [
        ("assign_group", "分配到组"),
        ("assign_user", "分配到人"),
        ("add_tag", "打标签"),
        ("set_status", "设置状态"),
    ]

    mailbox = models.ForeignKey(
        "accounts.Mailbox",
        on_delete=models.CASCADE,
        related_name="rules",
        help_text="该规则只对指定入口邮箱生效。",
    )
    priority = models.PositiveIntegerField(
        default=100, db_index=True, help_text="越小越优先。"
    )
    enabled = models.BooleanField(default=True)

    match_field = models.CharField(max_length=20, choices=MATCH_FIELD_CHOICES)
    match_op = models.CharField(max_length=20, choices=MATCH_OP_CHOICES)
    match_value = models.CharField(max_length=500)

    action_type = models.CharField(max_length=20, choices=ACTION_TYPE_CHOICES)
    action_value = models.CharField(max_length=500)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rules"
        verbose_name = "路由规则"
        verbose_name_plural = "路由规则"
        ordering = ["priority", "id"]

    def __str__(self) -> str:
        return f"#{self.priority} {self.get_match_field_display()}{self.get_match_op_display()}{self.match_value} → {self.get_action_type_display()}:{self.action_value}"

    # ---- 匹配（开发文档 §5.2：按优先级，第一个命中即停）----
    def _candidates(self, msg) -> list[str]:
        # 邮件头可能是 RFC2047 编码（=?utf-8?b?...?=），必须先解码再匹配，
        # 否则中文主题规则永远无法命中。
        if self.match_field == "subject":
            return [decode_mime_header(msg.get("Subject", ""))]
        if self.match_field == "from":
            raw = decode_mime_header(msg.get("From", ""))
            return [raw, extract_email(raw)]
        if self.match_field == "to":
            raw = decode_mime_header(msg.get("To", ""))
            return [raw, *extract_emails(raw)]
        if self.match_field == "body":
            return [plain_text_from_message(msg)]
        return []

    def matches(self, msg) -> bool:
        """判断邮件是否命中本规则。"""
        value = (self.match_value or "").strip()
        if not value:
            return False
        for candidate in self._candidates(msg):
            if not candidate:
                continue
            haystack = candidate.lower()
            needle = value.lower()
            if self.match_op == "contains" and needle in haystack:
                return True
            if self.match_op == "equals" and haystack.strip() == needle:
                return True
            if self.match_op == "domain":
                addresses = extract_emails(candidate) or [extract_email(candidate)]
                for addr in addresses:
                    if addr.endswith("@" + needle.lstrip("@")) or addr.split("@")[-1] == needle.lstrip("@"):
                        return True
            if self.match_op == "regex":
                try:
                    if re.search(value, candidate, re.IGNORECASE):
                        return True
                except re.error:
                    # 规则由管理员维护；非法正则不影响其他规则
                    logger.warning("规则 id=%s 的正则表达式非法，已跳过：%s", self.pk, value)
                    return False
        return False

    # ---- 动作 ----
    def resolve_group(self):
        """把规则动作解析为归属组（开发文档 §5.2 rule.resolve_group()）。"""
        from apps.accounts.models import Group, User

        value = (self.action_value or "").strip()
        if not value:
            return None
        if self.action_type == "assign_group":
            if value.isdigit():
                return Group.objects.filter(pk=int(value)).first()
            return Group.objects.filter(name=value).first()
        if self.action_type == "assign_user":
            user = None
            if value.isdigit():
                user = User.objects.filter(pk=int(value)).first()
            if user is None:
                user = User.objects.filter(username=value).first()
            if user is None:
                return None
            membership = user.user_groups.select_related("group").order_by("id").first()
            return membership.group if membership else None
        return None

    def resolved_user(self):
        """assign_user 动作解析出的用户。"""
        from apps.accounts.models import User

        if self.action_type != "assign_user":
            return None
        value = (self.action_value or "").strip()
        if not value:
            return None
        if value.isdigit():
            user = User.objects.filter(pk=int(value)).first()
            if user:
                return user
        return User.objects.filter(username=value).first()


class Template(models.Model):
    """自动回复模板（开发文档 §4.4）。

    `scope=group` 时按组覆盖全局模板（§2.4：全局一套 + 组可覆盖）。
    """

    SCOPE_CHOICES = [
        ("global", "全局"),
        ("group", "组覆盖"),
    ]

    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES)
    group = models.ForeignKey(
        "accounts.Group",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="templates",
        help_text="scope=group 时使用。",
    )
    body = models.TextField(help_text="支持 {{ ticket_id }} 等变量。")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "templates"
        verbose_name = "自动回复模板"
        verbose_name_plural = "自动回复模板"
        constraints = [
            # 备注：SQL 中 NULL 互不相等，该约束无法阻止多条"全局模板"，
            # 因此 save() 中补充了应用层校验（同 unique_fallback_mailbox 的处理方式）。
            models.UniqueConstraint(
                fields=["scope", "group"],
                name="unique_template_per_scope",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_scope_display()}模板（{self.group or '全局'}）"

    def clean(self) -> None:
        """scope 与 group 的配套关系（§10.1 模型先行）。"""
        super().clean()
        if self.scope == "global" and self.group_id is not None:
            raise ValidationError({"group": "全局模板不应绑定用户组。"})
        if self.scope == "group" and self.group_id is None:
            raise ValidationError({"group": "组覆盖模板必须指定用户组。"})

    def save(self, *args, **kwargs):
        if self.scope == "global":
            others = Template.objects.filter(scope="global")
            if self.pk:
                others = others.exclude(pk=self.pk)
            if others.exists():
                raise ValidationError({"scope": "全局模板只能存在一套（§2.4）。"})
            self.group_id = None
        return super().save(*args, **kwargs)
