"""apps.accounts：用户、组、组成员、邮箱配置（开发文档 §4.2）。

模型字段与约束严格对齐开发文档 v1.1 §4.2，未做增删。
"""

from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.crypto import decrypt_secret, encrypt_secret, mask_secret


class User(AbstractUser):
    """扩展 Django 内置 User，增加超级管理员标识。"""

    is_superadmin = models.BooleanField(
        default=False,
        help_text="可管理所有组、所有邮箱、所有配置。",
    )

    class Meta:
        db_table = "users"
        verbose_name = "用户"
        verbose_name_plural = "用户"

    # ---- 便捷查询（不改变字段，仅只读派生）----
    @property
    def group_ids(self) -> list[int]:
        return list(self.user_groups.values_list("group_id", flat=True))

    @property
    def is_group_admin(self) -> bool:
        """在任一组内拥有管理员身份。"""
        return self.user_groups.filter(is_admin=True).exists()

    @property
    def is_admin_group_member(self) -> bool:
        """属于任一管理员组（§2.7：管理员组可跨组查看）。"""
        return self.user_groups.filter(group__is_admin_group=True).exists()

    @property
    def sees_all_tickets(self) -> bool:
        return bool(
            self.is_superadmin or self.is_group_admin or self.is_admin_group_member
        )

    @property
    def display_name(self) -> str:
        return self.get_full_name() or self.username


class Mailbox(models.Model):
    """邮箱配置。连接飞书企业邮箱的 IMAP/SMTP。"""

    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)

    imap_host = models.CharField(max_length=255)
    imap_port = models.PositiveIntegerField(default=993)
    imap_ssl = models.BooleanField(default=True)

    smtp_host = models.CharField(max_length=255)
    smtp_port = models.PositiveIntegerField(default=465)
    smtp_ssl = models.BooleanField(default=True)

    username = models.CharField(max_length=255)
    secret_encrypted = models.BinaryField(
        help_text="Fernet 加密后的授权码/密码。",
    )

    # IMAP 增量同步状态
    last_uid = models.PositiveBigIntegerField(default=0)
    uidvalidity = models.PositiveBigIntegerField(default=0)

    is_fallback = models.BooleanField(
        default=False,
        help_text="全局兜底邮箱，全局唯一。",
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mailboxes"
        verbose_name = "邮箱"
        verbose_name_plural = "邮箱"
        ordering = ["id"]
        constraints = [
            # 注意：MariaDB/MySQL 不支持部分索引，Django 会静默跳过该约束
            # （django/db/backends/base/schema.py::_unique_supported）。
            # 因此 save()/clean() 中做了等价的应用层校验，保证"全局唯一兜底邮箱"。
            models.UniqueConstraint(
                fields=["is_fallback"],
                condition=models.Q(is_fallback=True),
                name="unique_fallback_mailbox",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"

    # ---- 凭据 ----
    def set_secret(self, plaintext: str) -> None:
        """加密写入授权码/密码，不落明文。"""
        self.secret_encrypted = encrypt_secret(plaintext)

    def get_secret(self) -> str:
        """解密读取授权码/密码。调用方不得写日志。"""
        return decrypt_secret(self.secret_encrypted)

    @property
    def secret_masked(self) -> str:
        """模板/日志展示用的掩码。"""
        return mask_secret("xxxxxxxx")

    # ---- 不变量：全局唯一兜底邮箱 ----
    def _assert_fallback_unique(self) -> None:
        if not self.is_fallback:
            return
        others = Mailbox.objects.filter(is_fallback=True)
        if self.pk:
            others = others.exclude(pk=self.pk)
        if others.exists():
            raise ValidationError(
                {"is_fallback": "已存在全局兜底邮箱，全局只能有一个（开发文档 §2.1）。"}
            )

    def clean(self) -> None:
        super().clean()
        self._assert_fallback_unique()

    def save(self, *args, **kwargs):
        self._assert_fallback_unique()
        if self.is_fallback:
            # 兜底邮箱必须启用，否则无邮箱组无法发信（§2.1 / §11.9）
            self.is_active = True
        return super().save(*args, **kwargs)

    @classmethod
    def get_fallback(cls) -> "Mailbox | None":
        """全局兜底邮箱（§4.6 fallback_mailbox_id 优先，其次 is_fallback 标记）。

        配置值非法（非数字/指向不存在的邮箱）时自动回退到 is_fallback 标记，
        避免因一条脏配置导致所有无邮箱组无法发信。
        """
        from apps.audit.models import Setting

        configured = Setting.get_optional_int("fallback_mailbox_id")
        if configured:
            mailbox = cls.objects.filter(pk=configured, is_active=True).first()
            if mailbox:
                return mailbox
        return cls.objects.filter(is_fallback=True, is_active=True).first()

    @property
    def owning_group(self) -> "Group | None":
        """该邮箱绑定的组；未绑定返回 None。

        反向 OneToOne 不存在时 Django 抛 RelatedObjectDoesNotExist
        （它同时继承 AttributeError），因此必须用 getattr 兜底。
        """
        return getattr(self, "group", None)

    @property
    def needs_auth_check(self) -> bool:  # pragma: no cover - 供巡检脚本使用
        return self.is_active and not self.secret_encrypted


class Group(models.Model):
    """用户组。可绑定一个对外邮箱，也可无邮箱。"""

    name = models.CharField(max_length=100, unique=True)

    mailbox = models.OneToOneField(
        Mailbox,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="group",
        help_text="该组的对外收发信邮箱。为空则走全局兜底邮箱。",
    )

    is_admin_group = models.BooleanField(
        default=False,
        help_text="管理员组，成员可跨组查看。",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "groups"
        verbose_name = "用户组"
        verbose_name_plural = "用户组"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def identity_email(self) -> str | None:
        """对外身份邮箱：组邮箱优先，其次全局兜底邮箱（§2.1）。"""
        if self.mailbox_id and self.mailbox and self.mailbox.is_active:
            return self.mailbox.email
        fallback = Mailbox.get_fallback()
        return fallback.email if fallback else None

    @property
    def identity_mailbox(self) -> Mailbox | None:
        """对外发信使用的邮箱对象：组邮箱优先，其次全局兜底邮箱。"""
        if self.mailbox_id and self.mailbox and self.mailbox.is_active:
            return self.mailbox
        return Mailbox.get_fallback()


class UserGroup(models.Model):
    """用户与组的成员关系。"""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="user_groups"
    )
    group = models.ForeignKey(
        Group, on_delete=models.CASCADE, related_name="user_groups"
    )
    is_admin = models.BooleanField(
        default=False,
        help_text="该用户在该组内的管理员身份。",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_groups"
        verbose_name = "组成员"
        verbose_name_plural = "组成员"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "group"],
                name="unique_user_group",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} @ {self.group}"
