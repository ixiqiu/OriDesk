"""URL 总入口。

路由约定（前后端不分离，Django 模板 + HTMX）：
- /                    工单收件箱（apps.tickets.urls）
- /api/mobile/         安卓客户端的角标与推送订阅端点（apps.notifications.urls）
- /accounts/           登录、用户与组管理（apps.accounts.urls）
- /routing/            规则与系统设置（apps.routing.urls）
- /autoresponder/      自动回复模板（apps.autoresponder.urls）
- /audit/              审计日志（apps.audit.urls）
- /admin/              Django Admin
- /healthz             健康检查
"""

from django.contrib import admin
from django.urls import include, path

from apps.core import views as core_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("routing/", include("apps.routing.urls")),
    path("autoresponder/", include("apps.autoresponder.urls")),
    path("audit/", include("apps.audit.urls")),
    # 移动端端点必须排在 `""` 之前：tickets 的路由挂在空前缀上，
    # 将来若新增通配规则会抢先吃掉 /api/mobile/...（契约 §1.2）。
    path("api/mobile/", include("apps.notifications.urls")),
    path("", include("apps.tickets.urls")),
    path("healthz", core_views.healthz, name="healthz"),
]

handler403 = "apps.core.views.forbidden"
handler404 = "apps.core.views.not_found"
handler500 = "apps.core.views.server_error"
# 注意：CSRF_FAILURE_VIEW 是 settings 项，不是 urls 项 —— 见 config/settings.py。
