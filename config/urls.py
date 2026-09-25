"""URL 总入口。

路由约定（前后端不分离，Django 模板 + HTMX）：
- /                    工单收件箱（apps.tickets.urls）
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
    path("", include("apps.tickets.urls")),
    path("healthz", core_views.healthz, name="healthz"),
]

handler403 = "apps.core.views.forbidden"
handler404 = "apps.core.views.not_found"
handler500 = "apps.core.views.server_error"
