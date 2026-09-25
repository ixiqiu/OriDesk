"""apps.audit：审计日志与系统配置（开发文档 §4.5 / §4.6）。"""

from __future__ import annotations

from django.core.cache import cache
from django.db import models

SETTING_CACHE_PREFIX = "setting:"
SETTING_CACHE_TTL = 30


class AuditLog(models.Model):
    ACTION_CHOICES = [
        ("reply", "回复"),
        ("forward", "改派"),
        ("claim", "认领"),
        ("unclaim", "取消认领"),
        ("config_change", "配置变更"),
        ("login", "登录"),
    ]

    user = models.ForeignKey(
        "accounts.User",
        null=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    ticket = models.ForeignKey(
        "tickets.Ticket",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    group = models.ForeignKey(
        "accounts.Group",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    identity_email = models.EmailField(
        blank=True, help_text="对外身份邮箱。"
    )
    detail = models.JSONField(
        default=dict, blank=True,
        help_text="收件人、主题摘要等结构化信息。",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "audit_logs"
        verbose_name = "审计日志"
        verbose_name_plural = "审计日志"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["ticket", "created_at"], name="audit_ticket_created_idx"),
            models.Index(fields=["user", "created_at"], name="audit_user_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} @ {self.created_at:%Y-%m-%d %H:%M}"


class Setting(models.Model):
    """系统配置，键值对（开发文档 §4.5 / §4.6）。"""

    key = models.CharField(max_length=100, unique=True)
    value = models.CharField(max_length=500, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    # 开发文档 §4.6 关键配置项默认值
    DEFAULTS: dict[str, str] = {
        "sticky_window_days": "7",
        "first_contact_window_hours": "24",
        "fallback_group_id": "",
        "fallback_mailbox_id": "",
        "max_attachment_size_mb": "25",
        "imap_poll_interval_seconds": "60",
    }

    class Meta:
        db_table = "settings"
        verbose_name = "系统设置"
        verbose_name_plural = "系统设置"
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key}={self.value}"

    # ---- 读写接口（开发文档 §5.2 使用 Setting.get(key, default)）----
    @classmethod
    def get(cls, key: str, default=None):
        """读取配置。返回值优先：数据库 > DEFAULTS > default。"""
        cache_key = f"{SETTING_CACHE_PREFIX}{key}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        row = cls.objects.filter(key=key).values_list("value", flat=True).first()
        if row is None or row == "":
            value = cls.DEFAULTS.get(key, default)
        else:
            value = row
        if value is not None:
            cache.set(cache_key, value, SETTING_CACHE_TTL)
        return value

    @classmethod
    def get_int(cls, key: str, default: int | None = None) -> int | None:
        try:
            raw = cls.get(key, default)
            return None if raw in (None, "") else int(raw)
        except (TypeError, ValueError):
            return default

    @classmethod
    def get_bool(cls, key: str, default: bool = False) -> bool:
        raw = cls.get(key, default)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    @classmethod
    def get_optional_int(cls, key: str) -> int | None:
        raw = cls.get(key)
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @classmethod
    def set(cls, key: str, value, *, user=None) -> "Setting":
        """写入配置并写审计（谁在何时改了哪一项，§2.5）。"""
        from apps.audit.services import record

        text = "" if value is None else str(value)
        row, created = cls.objects.update_or_create(
            key=key, defaults={"value": text}
        )
        cache.delete(f"{SETTING_CACHE_PREFIX}{key}")
        record(
            user=user,
            action="config_change",
            detail={
                "key": key,
                "value": text,
                "created": created,
            },
        )
        return row

    @classmethod
    def bulk_set(cls, values: dict, *, user=None) -> int:
        """批量写入配置（会逐项失效缓存并写审计）。

        注意：**不要**用 `Setting.objects.filter(...).update(...)` 直接改值——
        Django 的 `QuerySet.update()` 既不触发 `save()` 也不发信号，缓存不会失效，
        最长 30 秒（SETTING_CACHE_TTL）内各进程仍可能读到旧值。
        """
        count = 0
        for key, value in values.items():
            cls.set(key, value, user=user)
            count += 1
        return count

    def save(self, *args, **kwargs):
        result = super().save(*args, **kwargs)
        cache.delete(f"{SETTING_CACHE_PREFIX}{self.key}")
        return result

    def delete(self, *args, **kwargs):
        cache.delete(f"{SETTING_CACHE_PREFIX}{self.key}")
        return super().delete(*args, **kwargs)
