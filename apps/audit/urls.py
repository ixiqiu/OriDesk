"""audit URL：审计日志查询。"""

from django.urls import path

from apps.audit import views

app_name = "audit"

urlpatterns = [
    path("", views.log_list, name="log_list"),
    path("<int:pk>/", views.log_detail, name="log_detail"),
]
