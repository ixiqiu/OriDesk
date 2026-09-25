"""accounts URL（登录、个人设置、用户/组/邮箱管理）。

URL 名称是本项目的对外契约，视图实现见 apps/accounts/views.py。
"""

from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="accounts/login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "password/",
        auth_views.PasswordChangeView.as_view(
            template_name="accounts/password_change.html",
            success_url=reverse_lazy("accounts:password_change_done"),
        ),
        name="password_change",
    ),
    path(
        "password/done/",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="accounts/password_change_done.html"
        ),
        name="password_change_done",
    ),
    path("profile/", views.profile, name="profile"),
    path("users/", views.user_list, name="user_list"),
    path("users/new/", views.user_create, name="user_create"),
    path("users/<int:pk>/edit/", views.user_edit, name="user_edit"),
    path("groups/", views.group_list, name="group_list"),
    path("groups/new/", views.group_create, name="group_create"),
    path("groups/<int:pk>/edit/", views.group_edit, name="group_edit"),
    path("mailboxes/", views.mailbox_list, name="mailbox_list"),
    path("mailboxes/new/", views.mailbox_create, name="mailbox_create"),
    path("mailboxes/<int:pk>/edit/", views.mailbox_edit, name="mailbox_edit"),
    path("mailboxes/<int:pk>/verify/", views.mailbox_verify, name="mailbox_verify"),
]
