"""推送订阅的 Admin。

存在的理由：排查「这台设备还活着吗 / 到底注册了几台」时不必连数据库看表。
`topic` 设为只读 —— 它是安全边界（谁拿到谁能收推送），不该在后台被手滑改掉。
"""

from django.contrib import admin

from apps.notifications.models import Subscription


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "device_label",
        "enabled",
        "server",
        "last_seen_at",
        "created_at",
    )
    list_filter = ("enabled",)
    search_fields = ("user__username", "device_label", "topic")
    raw_id_fields = ("user",)
    readonly_fields = ("topic", "created_at")
    ordering = ("-created_at",)
