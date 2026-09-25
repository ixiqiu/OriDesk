"""把当前请求用户放进 contextvar，便于服务层写审计时不必层层传 user。"""

from __future__ import annotations

from apps.audit.services import set_current_user


class AuditContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        set_current_user(getattr(request, "user", None))
        try:
            return self.get_response(request)
        finally:
            set_current_user(None)
