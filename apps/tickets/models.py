"""apps.tickets：工单、消息、附件（开发文档 §4.3）。

字段与约束严格对齐 §4.3；`last_message_id` / `references_header` 等为只读派生属性，
不新增数据库字段。
"""

from __future__ import annotations

from django.db import models
from django.urls import reverse

# 危险扩展名：仅可下载，不允许在线预览（开发文档 §6.3）
DANGEROUS_EXTENSIONS = {".exe", ".bat", ".js", ".scr", ".vbs", ".cmd", ".com", ".msi", ".jar", ".ps1"}


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
    def extension(self) -> str:
        _, _, ext = self.filename.rpartition(".")
        return f".{ext.lower()}" if ext else ""

    @property
    def is_dangerous(self) -> bool:
        """危险扩展名标记为不可预览，仅可下载（§6.3）。"""
        return self.extension in DANGEROUS_EXTENSIONS

    @property
    def size_display(self) -> str:
        size = float(self.size or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{self.size} B"
