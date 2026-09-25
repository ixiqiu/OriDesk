"""routing URL：规则维护与系统设置。"""

from django.urls import path

from apps.routing import views

app_name = "routing"

urlpatterns = [
    path("", views.rule_list, name="rule_list"),
    path("rules/new/", views.rule_create, name="rule_create"),
    path("rules/<int:pk>/edit/", views.rule_edit, name="rule_edit"),
    path("rules/<int:pk>/delete/", views.rule_delete, name="rule_delete"),
    path("rules/<int:pk>/toggle/", views.rule_toggle, name="rule_toggle"),
    path("settings/", views.settings_edit, name="settings"),
    path("mailboxes/", views.mailbox_overview, name="mailbox_overview"),
    path("tags/", views.tag_admin_list, name="tag_admin_list"),
    path("tags/new/", views.tag_admin_edit, name="tag_admin_create"),
    path("tags/<int:pk>/edit/", views.tag_admin_edit, name="tag_admin_edit"),
    path("tags/<int:pk>/delete/", views.tag_admin_delete, name="tag_admin_delete"),
    path("tags/<int:pk>/toggle/", views.tag_admin_toggle, name="tag_admin_toggle"),
]
