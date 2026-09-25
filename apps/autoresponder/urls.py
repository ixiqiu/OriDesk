"""autoresponder URL：自动回复模板维护（全局 + 组覆盖）。"""

from django.urls import path

from apps.autoresponder import views

app_name = "autoresponder"

urlpatterns = [
    path("", views.template_list, name="template_list"),
    path("global/", views.template_edit, {"scope": "global"}, name="template_edit"),
    path("group/<int:pk>/", views.template_edit, name="group_template_edit"),
    path("group/<int:pk>/delete/", views.template_delete, name="group_template_delete"),
]
