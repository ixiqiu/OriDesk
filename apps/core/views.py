"""通用视图：健康检查与错误页。"""

from __future__ import annotations

from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache


@never_cache
def healthz(request):
    """健康检查：进程存活 + 数据库可达。"""
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # pragma: no cover - 仅在数据库故障时触发
        db_ok = False
    status = 200 if db_ok else 503
    response = JsonResponse({"status": "ok" if db_ok else "degraded", "database": db_ok})
    response.status_code = status
    return response


def forbidden(request, exception=None):
    return render(request, "errors/403.html", status=403)


def not_found(request, exception=None):
    return render(request, "errors/404.html", status=404)


def server_error(request):
    return render(request, "errors/500.html", status=500)


API_PATH_PREFIX = "/api/"


def csrf_failure(request, reason=""):
    """CSRF 校验失败的统一入口（契约 §2.2 / §3.0）。

    Django 默认渲染 403 HTML 页。对浏览器没问题，但移动端 App 需要的是
    `{"error": {"code": "csrf_failed"}}` 这样的**机器可读**响应 —— 否则 App
    只能看到一坨 HTML，无法区分「CSRF 过期」与「真的没权限」，
    而这两者的处理方式完全不同（前者重取 cookie 后重试一次，后者终止）。

    Web 页面路径保持原样（渲染 403.html），不改既有行为。
    """
    if request.path.startswith(API_PATH_PREFIX):
        return JsonResponse(
            {
                "error": {
                    "code": "csrf_failed",
                    "message": "CSRF 校验失败，请重新打开应用后重试。",
                }
            },
            status=403,
        )
    return forbidden(request, reason)
