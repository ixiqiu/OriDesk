"""tickets URL。

收件箱用 scope 参数区分视图（全部 / 我的 / 待认领 / 待回复）。
"""

from django.urls import path

from apps.tickets import views

app_name = "tickets"

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("mine/", views.inbox, {"scope": "mine"}, name="mine"),
    path("unassigned/", views.inbox, {"scope": "unassigned"}, name="unassigned"),
    path("awaiting/", views.inbox, {"scope": "awaiting"}, name="awaiting"),
    path("tickets/<int:pk>/", views.detail, name="detail"),
    path("tickets/<int:pk>/reply/", views.reply, name="reply"),
    path("tickets/<int:pk>/note/", views.create_note, name="note"),
    path("tickets/<int:pk>/claim/", views.claim, name="claim"),
    path("tickets/<int:pk>/unclaim/", views.unclaim, name="unclaim"),
    path("tickets/<int:pk>/reassign/", views.reassign, name="reassign"),
    path("tickets/<int:pk>/status/", views.update_status, name="set_status"),
    path("tickets/<int:pk>/messages/", views.message_list, name="message_list"),
    path("attachments/<int:pk>/download/", views.attachment_download, name="attachment_download"),
    path("attachments/<int:pk>/preview/", views.attachment_preview, name="attachment_preview"),
]
