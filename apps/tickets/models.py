"""apps.tickets：工单、消息、附件（开发文档 §4.3）。

字段与约束严格对齐 §4.3；`last_message_id` / `references_header` 等为只读派生属性，
不新增数据库字段。
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse

# 危险扩展名：仅可下载，不允许在线预览（开发文档 §6.3）
DANGEROUS_EXTENSIONS = {".exe", ".bat", ".js", ".scr", ".vbs", ".cmd", ".com", ".msi", ".jar", ".ps1"}

# 可安全内联预览的类型白名单（存储型 XSS 防护，见 docs/安全清单核查.md R-3）：
# - HTML / SVG / XHTML / XML 等"活动内容"一律不在白名单内，只能下载；
# - 其余未知类型也一律不可预览；
# - 预览响应固定带 X-Content-Type-Options: nosniff，避免浏览器把伪装成图片的文件按 HTML 解析。
PREVIEWABLE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "image/bmp",
    "text/plain",
    "application/pdf",
}

# 扩展名 → 兜底 MIME（当附件没有 MIME 头时使用）
SAFE_EXTENSION_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/plain",
    ".pdf": "application/pdf",
}


class Ticket(models.Model):
    STATUS_CHOICES = [
        ("open", "进行中"),
        ("pending", "等待客户"),
        ("closed", "已关闭"),
    ]

    mailbox = models.ForeignKey(
        "accounts.Mailbox",
        on_delete=models.PROTECT,
        related_name="tickets",
        help_text="入口邮箱。",
    )
    group = models.ForeignKey(
        "accounts.Group",
        on_delete=models.PROTECT,
        related_name="tickets",
        help_text="当前归属组。",
    )
    assignee = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_tickets",
        help_text="认领人。",
    )

    subject = models.CharField(max_length=500)
    normalized_subject = models.CharField(
        max_length=500,
        db_index=True,
        help_text="剥离 Re:/Fwd:/工单号后的纯净主题。",
    )

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="open"
    )
    is_awaiting_reply = models.BooleanField(
        default=False,
        db_index=True,
        help_text="待回复标签。客户来信置 True，外发回复置 False。",
    )

    customer_email = models.EmailField(db_index=True)

    last_message_at = models.DateTimeField(
        db_index=True,
        help_text="最后一条消息时间。用于粘性判断和列表排序。",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tickets"
        verbose_name = "工单"
        verbose_name_plural = "工单"
        ordering = ["-last_message_at"]
        indexes = [
            models.Index(fields=["group", "is_awaiting_reply"], name="ticket_group_awaiting_idx"),
            models.Index(fields=["customer_email", "last_message_at"], name="ticket_cust_last_idx"),
        ]

    def __str__(self) -> str:
        return f"[T#{self.pk}] {self.subject}"

    def get_absolute_url(self) -> str:
        return reverse("tickets:detail", args=[self.pk])

    # ---- 只读派生属性（不新增字段）----
    @property
    def last_message_id(self) -> str:
        """最后一条入站邮件的 Message-ID，用于回信 In-Reply-To（§5.4）。"""
        message = (
            self.messages.filter(direction="in")
            .exclude(message_id="")
            .order_by("-created_at", "-id")
            .only("message_id")
            .first()
        )
        return message.message_id if message else ""

    @property
    def references_header(self) -> str:
        """整条会话的 Message-ID 链，用于回信 References（§5.4 ticket.references）。"""
        ids = [
            message_id
            for message_id in self.messages.exclude(message_id="")
            .order_by("created_at", "id")
            .values_list("message_id", flat=True)
        ]
        return " ".join(ids[-20:])

    @property
    def identity_mailbox(self):
        """对外身份邮箱：组邮箱优先，无邮箱组走全局兜底（§2.1 / §5.4）。"""
        return self.group.identity_mailbox if self.group_id else None

    @property
    def pending_label(self) -> str:
        """组内协作提示（§2.6）：认领后只有认领人看到"待回复"。"""
        if not self.is_awaiting_reply:
            return ""
        if self.assignee_id:
            return "待回复（认领人）"
        return "待回复"


class Message(models.Model):
    DIRECTION_CHOICES = [
        ("in", "收信"),
        ("out", "发信"),
    ]
    TYPE_CHOICES = [
        ("message", "邮件"),
        ("note", "内部备注"),
    ]

    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name="messages"
    )
    mailbox = models.ForeignKey(
        "accounts.Mailbox",
        on_delete=models.PROTECT,
        related_name="messages",
    )

    message_id = models.CharField(max_length=500, db_index=True, blank=True)
    in_reply_to = models.CharField(max_length=500, blank=True)
    references = models.TextField(blank=True)

    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    type = models.CharField(
        max_length=10, choices=TYPE_CHOICES, default="message"
    )

    from_addr = models.EmailField(blank=True)
    to_addr = models.TextField(blank=True)
    cc_addr = models.TextField(blank=True)

    subject = models.CharField(max_length=500, blank=True)
    body_text = models.TextField(blank=True)
    body_html = models.TextField(blank=True)

    actual_sender = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_messages",
        help_text="内部实际发件人。对外身份由 mailbox 决定。",
    )
    is_auto_reply = models.BooleanField(default=False)

    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "messages"
        verbose_name = "消息"
        verbose_name_plural = "消息"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["ticket", "created_at"], name="msg_ticket_created_idx"),
            models.Index(fields=["message_id"], name="msg_message_id_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_direction_display()} {self.from_addr} → {self.to_addr}"

    @property
    def is_note(self) -> bool:
        return self.type == "note"

    @property
    def has_attachments(self) -> bool:
        return self.attachments.exists()

    @property
    def preview(self) -> str:
        text = (self.body_text or "").strip().replace("\n", " ")
        return text[:120] + ("…" if len(text) > 120 else "")


class Attachment(models.Model):
    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, related_name="attachments"
    )
    filename = models.CharField(max_length=500)
    mime = models.CharField(max_length=200, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    path = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "attachments"
        verbose_name = "附件"
        verbose_name_plural = "附件"
        ordering = ["id"]

    def __str__(self) -> str:
        return self.filename

    @property
    def normalized_filename(self) -> str:
        """规范化文件名：去掉控制字符，并剥离尾部空格与点。

        Windows 与多数浏览器保存时会**丢弃**文件名结尾的空格与点，
        因此 `tool.exe.` / `tool.exe ` 实际落地为 `tool.exe`，
        若不规范化就会绕过危险扩展名判断（已由 tests/test_fuzz_security.py 覆盖）。
        """
        name = (self.filename or "").replace("\\", "/").split("/")[-1]
        name = "".join(ch for ch in name if ch.isprintable() or ch == " ")
        return name.strip().rstrip(". ").strip()

    @property
    def extension(self) -> str:
        name = self.normalized_filename
        _, _, ext = name.rpartition(".")
        return f".{ext.lower()}" if ext and ext != name else ""

    @property
    def is_dangerous(self) -> bool:
        """危险扩展名标记为不可预览，仅可下载（§6.3）。"""
        return self.extension in DANGEROUS_EXTENSIONS

    @property
    def effective_mime(self) -> str:
        """用于响应的 MIME：以白名单为准，未知/缺失类型按扩展名兜底。"""
        mime = (self.mime or "").split(";")[0].strip().lower()
        if mime in PREVIEWABLE_MIME_TYPES:
            return mime
        return SAFE_EXTENSION_MIME.get(self.extension, "")

    @property
    def is_previewable(self) -> bool:
        """能否在线预览：仅白名单内的安全类型（HTML/SVG/脚本等一律只能下载）。"""
        if self.is_dangerous:
            return False
        return bool(self.effective_mime) and self.effective_mime in PREVIEWABLE_MIME_TYPES

    @property
    def size_display(self) -> str:
        size = float(self.size or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{self.size} B"


# ------------------------------------------------------------------ 标签（v1.2 新增）
# 背景：开发文档 §4.4 的 Rule.ACTION_TYPE_CHOICES 含 add_tag，但 §4.3 的冻死模型没有标签字段，
# 规则命中后无处落库。经人工确认（§10.4「新增模型需人工确认」）后按以下方案落地：
#   Tag        —— 标签字典：group 为空=全局标签，否则为该组专属标签（组内同名唯一）
#   TicketTag  —— 工单↔标签关联：记录打标签的人、来源（规则/手动）与时间
# 单工单标签数量上限（防止标签被当成备注滥用，也保证界面不失控）
MAX_TAGS_PER_TICKET = 20

TAG_COLOR_CHOICES = [
    ("slate", "灰"),
    ("blue", "蓝"),
    ("green", "绿"),
    ("amber", "黄"),
    ("red", "红"),
    ("purple", "紫"),
    ("teal", "青"),
]


class Tag(models.Model):
    """标签字典（§4.4 add_tag 动作的落库载体）。"""

    name = models.CharField(max_length=50)
    group = models.ForeignKey(
        "accounts.Group",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="tags",
        help_text="为空表示全局标签（所有组可用）；否则为该组专属标签。",
    )
    color = models.CharField(
        max_length=20, choices=TAG_COLOR_CHOICES, default="slate", help_text="界面角标配色。"
    )
    description = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tags"
        verbose_name = "标签"
        verbose_name_plural = "标签"
        ordering = ["name", "id"]
        constraints = [
            # 组内同名唯一（group 非空时由数据库保证；group 为空的"全局标签"因 SQL 中
            # NULL 互不相等，需由 clean()/save() 兜底，与 Mailbox 兜底邮箱同一处理方式）。
            models.UniqueConstraint(
                fields=["group", "name"],
                name="unique_tag_per_group",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name}（{self.group.name}）" if self.group_id else f"{self.name}（全局）"

    @staticmethod
    def normalize_name(name: str) -> str:
        """标签名归一化：去首尾空白、压缩内部连续空白。"""
        return re.sub(r"\s+", " ", (name or "").strip())

    def _assert_unique_in_scope(self) -> None:
        name = self.normalize_name(self.name)
        if not name:
            raise ValidationError({"name": "标签名不能为空。"})
        # 用 iexact 与 MariaDB 的 utf8mb4_unicode_ci 归并规则保持一致：
        # 生产库上 "Urgent" 与 "urgent" 会撞唯一约束，若这里用精确匹配就会
        # 绕过应用层校验直接抛 IntegrityError（表现为 500 而不是表单错误）。
        others = Tag.objects.filter(name__iexact=name)
        if self.group_id:
            others = others.filter(group_id=self.group_id)
        else:
            others = others.filter(group__isnull=True)
        if self.pk:
            others = others.exclude(pk=self.pk)
        if others.exists():
            scope = f"用户组「{self.group.name}」" if self.group_id else "全局"
            raise ValidationError({"name": f"{scope}已存在同名标签「{name}」。"})

    def clean(self) -> None:
        super().clean()
        self.name = self.normalize_name(self.name)
        self._assert_unique_in_scope()

    def save(self, *args, **kwargs):
        self.name = self.normalize_name(self.name)
        self._assert_unique_in_scope()
        return super().save(*args, **kwargs)

    @property
    def scope_label(self) -> str:
        return self.group.name if self.group_id else "全局"

    def is_available_for(self, group) -> bool:
        """该标签是否可用于指定组的工单：全局标签人人可用，组标签仅限本组。"""
        if not self.is_active:
            return False
        if self.group_id is None:
            return True
        return group is not None and self.group_id == group.pk

    @property
    def ticket_count(self) -> int:
        return self.ticket_tags.count()


class TicketTag(models.Model):
    """工单与标签的关联（谁、何时、以什么方式打的标签）。"""

    SOURCE_CHOICES = [
        ("rule", "规则自动"),
        ("manual", "人工添加"),
    ]

    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name="ticket_tags"
    )
    tag = models.ForeignKey(
        Tag, on_delete=models.CASCADE, related_name="ticket_tags"
    )
    added_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="added_ticket_tags",
        help_text="人工添加时的操作人；规则添加为空。",
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ticket_tags"
        verbose_name = "工单标签"
        verbose_name_plural = "工单标签"
        ordering = ["tag__name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["ticket", "tag"],
                name="unique_ticket_tag",
            )
        ]

    def __str__(self) -> str:
        return f"[T#{self.ticket_id}] {self.tag.name}"

    @property
    def is_foreign(self) -> bool:
        """是否为"历史标签"：改派后仍保留的原组专属标签。

        改派不删除标签（保留历史事实、避免信息丢失），但该标签对当前组不可用，
        界面上应以弱化样式标注为历史标签，且不再出现在可选下拉中。
        """
        if self.tag.group_id is None:
            return False
        return self.tag.group_id != self.ticket.group_id
