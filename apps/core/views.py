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
