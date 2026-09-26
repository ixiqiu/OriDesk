"""推送订阅模型（契约 §5.1 / 决策 D2）。

本模型属开发文档 §10.4「新增模型需人工确认」范围，已按
`docs/移动端API约定.md` §5.1 的字段表经人工确认（D2）后落地。

安全要点（契约 §4.3）：
- **topic 本质就是密码**。ntfy 没有注册机制，谁猜到 topic 谁就能收到推送，
  所以 topic 必须由服务端生成足够随机的字符串，且全局唯一（防串频道）。
- 本表**不含任何 ntfy token 字段**。token 是**实例级**配置，存在 Setting 里
  并以 Fernet 密文保存（决策 D4），不是每设备一份。
"""

from __future__ import annotations

import re
import secrets

from django.conf import settings
from django.db import models

TOPIC_PATTERN = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
"""ntfy 允许的 topic 字符集与长度（已核官方文档：仅 `[-_A-Za-z0-9]`，最长 64）。"""

TOPIC_TOKEN_BYTES = 24
"""`secrets.token_urlsafe(24)` 产出 32 个字符（24 能被 3 整除，无 padding）。"""

DEFAULT_TOPIC_PREFIX = "oridesk"
MAX_DEVICE_LABEL = 64


def sanitize_topic_prefix(prefix: str | None) -> str:
    """把设置项里的前缀清洗成合法 topic 片段。"""
    cleaned = re.sub(r"[^\-_A-Za-z0-9]", "", prefix or "")
    return (cleaned or DEFAULT_TOPIC_PREFIX)[:16]


def generate_topic(prefix: str | None = None) -> str:
    """生成一个不可枚举的随机 topic。

    用 `secrets.token_urlsafe` 而不是 `uuid4`：它的字母表恰是 base64url
    （`A-Za-z0-9-_`），**天然落在 ntfy 允许的字符集内**，不需要额外清洗，
    也就不会出现"清洗后熵变低"这种隐患。
    """
    return f"{sanitize_topic_prefix(prefix)}-{secrets.token_urlsafe(TOPIC_TOKEN_BYTES)}"


class Subscription(models.Model):
    """一台设备的推送订阅。"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
        help_text="归属用户。用户被删除时订阅一并消失。",
    )
    topic = models.CharField(
        max_length=64,
        unique=True,
        help_text="服务端生成的随机 ntfy topic。全局唯一，防止串频道。",
    )
    server = models.CharField(
        max_length=255,
        blank=True,
        help_text="注册当时的 ntfy 实例地址（冗余记录，便于排查换域名后旧订阅失效）。",
    )
    device_label = models.CharField(
        max_length=MAX_DEVICE_LABEL,
        blank=True,
        help_text="便于用户在设置里辨认并吊销，例如「Pixel 7 · 小邱」。",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="关闭后后端不再向该设备发布。",
    )
    dnd_start = models.TimeField(
        null=True,
        blank=True,
        help_text="免打扰开始时刻。与 dnd_end 都非空才视为启用。",
    )
    dnd_end = models.TimeField(
        null=True,
        blank=True,
        help_text="免打扰结束时刻。",
    )
    last_seen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="最后一次续订（E3）时间。用于判断「这台设备还活着吗」。",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "push_subscriptions"
        verbose_name = "推送订阅"
        verbose_name_plural = "推送订阅"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "topic"], name="uniq_subscription_user_topic"
            ),
        ]
        indexes = [
            models.Index(fields=["user", "enabled"], name="push_sub_user_enabled_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.topic[:12]}…"

    @property
    def dnd_enabled(self) -> bool:
        """免打扰时段是否已配置（两端都填了才算）。"""
        return self.dnd_start is not None and self.dnd_end is not None

    def is_within_dnd(self, moment=None) -> bool:
        """给定时刻是否落在免打扰时段内。

        支持跨午夜（例如 22:00–08:00）。

        **注意（重要设计决定）**：服务端**不会**用这个方法去抑制推送。
        原因是抑制会让消息根本不进 ntfy —— 免打扰结束后用户永远看不到它。
        正确语义是「不打扰」而不是「不告诉我」，因此本字段是**同步给客户端的偏好**：
        App 从 E2 读到后自行决定不弹通知，但消息仍会到达并被 ntfy 缓存，
        角标也能照常更新。方法保留给客户端契约做参考实现，避免两端理解不一致。
        """
        if not self.dnd_enabled:
            return False
        from django.utils import timezone

        moment = moment or timezone.localtime()
        current = moment.time()
        if self.dnd_start <= self.dnd_end:
            return self.dnd_start <= current < self.dnd_end
        # 跨午夜：22:00–08:00 表示 current >= 22:00 或 current < 08:00
        return current >= self.dnd_start or current < self.dnd_end

    def record_seen(self, when=None) -> None:
        """E3 续订时刷新 last_seen_at（只更新这一列，不触发全字段写）。"""
        from django.utils import timezone

        self.last_seen_at = when or timezone.now()
        self.save(update_fields=["last_seen_at"])
