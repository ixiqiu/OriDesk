"""apps.accounts：用户、组、成员关系、权限。"""

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "账号与权限"

    def ready(self) -> None:
        """登录审计（开发文档 §4.5 audit action=login）。"""
        from django.contrib.auth.signals import user_logged_in

        from apps.audit.services import record

        def _audit_login(sender, request, user, **kwargs):  # noqa: ANN001
            record(
                user=user,
                action="login",
                detail={
                    "username": user.username,
                    "ip": (request.META.get("REMOTE_ADDR") if request is not None else "") or "",
                    "user_agent": (request.META.get("HTTP_USER_AGENT", "") if request is not None else "")[:200],
                },
            )

        user_logged_in.connect(_audit_login, dispatch_uid="accounts.audit_login")
