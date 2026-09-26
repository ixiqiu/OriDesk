"""移动端端点 URL（契约 §1.2，决策 D7：前缀 `/api/mobile/`）。

**挂载位置很重要**：`config/urls.py` 里这一条必须放在
`path("", include("apps.tickets.urls"))` **之前**，否则将来 tickets 若新增通配路由
会抢先匹配掉 `/api/mobile/...`。
"""

from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    # E1 角标（顺带下发 CSRF cookie，决策 D9）
    path("badge/", views.badge, name="badge"),
    # E2 列表 / E3 注册续订
    path("subscriptions/", views.subscriptions, name="subscriptions"),
    # E4 修改 / E5 吊销
    path(
        "subscriptions/<int:pk>/",
        views.subscription_detail,
        name="subscription_detail",
    ),
    # E6 测试推送
    path(
        "subscriptions/<int:pk>/test/",
        views.subscription_test,
        name="subscription_test",
    ),
]
