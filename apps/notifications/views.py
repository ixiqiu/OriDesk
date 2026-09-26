"""移动端端点 E1–E6（契约 §3）。

| # | 方法 | 路径 | 用途 |
|---|---|---|---|
| E1 | GET | `/api/mobile/badge/` | 角标计数；顺带下发 CSRF cookie |
| E2 | GET | `/api/mobile/subscriptions/` | 本用户的订阅列表 |
| E3 | POST | `/api/mobile/subscriptions/` | 注册/续订本设备（幂等 upsert） |
| E4 | PATCH | `/api/mobile/subscriptions/<id>/` | 改 enabled / 免打扰 / 标签 |
| E5 | DELETE | `/api/mobile/subscriptions/<id>/` | 吊销本设备订阅 |
| E6 | POST | `/api/mobile/subscriptions/<id>/test/` | 发一条测试推送 |

三条贯穿全部端点的约定：

1. **未登录返回 401 JSON，不是 302**（`api_login_required`，契约 §2.2）。
2. **不可见 / 非本人的对象一律 404，不是 403**（契约 §2.4）——
   403 会泄露「这个 id 存在」。
3. **错误体恒为 `{"error": {"code", "message"}}`**（契约 §3.0），
   `code` 给程序分支，`message` 给用户看且**不含任何敏感信息**。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from functools import wraps

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.permissions import api_login_required
from apps.notifications import ntfy
from apps.notifications.models import MAX_DEVICE_LABEL, Subscription, generate_topic
from apps.tickets.selectors import scope_counts

logger = logging.getLogger(__name__)


class _ApiError(Exception):
    """带 HTTP 状态与错误码的内部异常，由 `_json_endpoint` 统一转成错误体。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _error(code: str, message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def _json_endpoint(view_func):
    """把 `_ApiError` 转成契约 §3.0 的错误体。"""

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        try:
            return view_func(request, *args, **kwargs)
        except _ApiError as exc:
            return _error(exc.code, exc.message, exc.status)

    return _wrapped


def _json_body(request) -> dict:
    """解析请求体。空体视为 `{}`，便于 E4 之外的无参 POST。"""
    raw = request.body or b""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise _ApiError("bad_request", "请求体不是合法的 JSON。") from exc
    if not isinstance(data, dict):
        raise _ApiError("bad_request", "请求体必须是 JSON 对象。")
    return data


def _as_bool(value) -> bool:
    """宽松布尔解析。

    不能直接 `bool(value)`：App 若把 `false` 序列化成字符串 `"false"`，
    `bool("false")` 是 `True` —— 那会把「关闭推送」变成「打开推送」。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _parse_time(value, field: str):
    """解析 `"HH:MM"`；`None`/空串表示清除。"""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise _ApiError("validation_error", f"{field} 必须是 \"HH:MM\" 字符串或 null。")
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise _ApiError(
            "validation_error", f"{field} 必须是 24 小时制的 \"HH:MM\"。"
        ) from exc


def _owned_or_404(user, pk: int) -> Subscription:
    """取本人的订阅；不是本人的一律 404（不泄露该 id 是否存在）。"""
    sub = Subscription.objects.filter(pk=pk, user=user).first()
    if sub is None:
        raise _ApiError("not_found", "订阅不存在或无权访问。", 404)
    return sub


def _serialize(sub: Subscription) -> dict:
    """订阅的外部表示。

    **绝不包含 ntfy token**：它是实例级发布凭据，与"这台设备是谁"无关，
    泄露给客户端毫无必要（契约 §3.2）。
    """
    return {
        "id": sub.pk,
        "topic": sub.topic,
        "server": sub.server,
        "device_label": sub.device_label,
        "enabled": sub.enabled,
        "dnd_start": sub.dnd_start.strftime("%H:%M") if sub.dnd_start else None,
        "dnd_end": sub.dnd_end.strftime("%H:%M") if sub.dnd_end else None,
        "last_seen_at": sub.last_seen_at.isoformat() if sub.last_seen_at else None,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
    }


# ------------------------------------------------------------------ E1
@require_GET
@ensure_csrf_cookie
@api_login_required
@_json_endpoint
def badge(request):
    """角标计数（契约 §3.1）。

    `@ensure_csrf_cookie` 让本端点**顺带下发 `csrftoken` cookie**（决策 D9）：
    App 启动后先调 E1，就能同时拿到角标数字与后续写操作要用的 CSRF 令牌，
    省掉一个专门的 `/api/mobile/csrf/` 端点。

    计数**复用 `selectors.scope_counts`**，与收件箱页 chips 是同一份口径 ——
    否则会出现"角标显示 3、点进去 2 条"这种自相矛盾（决策 D5）。
    """
    counts = scope_counts(request.user)
    return JsonResponse(
        {
            "awaiting": counts["awaiting"],
            "unassigned": counts["unassigned"],
            "mine": counts["mine"],
            "all": counts["all"],
            "generated_at": timezone.now().isoformat(),
        }
    )


# ------------------------------------------------------------------ E2 / E3
@require_http_methods(["GET", "POST"])
@api_login_required
@_json_endpoint
def subscriptions(request):
    """`/api/mobile/subscriptions/` —— 列表（GET）与注册（POST）共用一条路径。

    Django 的 `path()` 一条 URL 只能映射一个可调用对象，所以按方法分派。
    装饰器集中在**分派器**上，实现函数保持无装饰，避免重复鉴权。
    """
    if request.method == "POST":
        return _create_subscription(request)
    return _list_subscriptions(request)


def _list_subscriptions(request):
    subs = request.user.push_subscriptions.all()
    return JsonResponse({"subscriptions": [_serialize(s) for s in subs]})


def _create_subscription(request):
    """注册/续订本设备（幂等 upsert，契约 §3.3）。

    **topic 由服务端生成**（`generate_topic`），App 不能自造 —— topic 本质是密码，
    让客户端挑就等于允许它挑一个可猜测的值，或撞进别人的频道。

    但 App **可以回传服务端此前下发给它的 topic**（"回显"而非"自选"），
    用于网络重试与重装后的续订；服务端会校验该 topic 确属本人，否则 404。
    没有这条，一次重试就会多出一条订阅记录，而 App 只知道其中一条。
    """
    data = _json_body(request)
    label = str(data.get("device_label") or "").strip()[:MAX_DEVICE_LABEL]
    echoed = str(data.get("topic") or "").strip()

    if echoed:
        sub = Subscription.objects.filter(user=request.user, topic=echoed).first()
        if sub is None:
            return _error("not_found", "订阅不存在或无权访问。", 404)
        # 只刷新可变字段与"还活着"的时间戳；**不覆盖 enabled**——
        # 用户主动关掉推送的偏好不该被一次续订悄悄改回来。
        sub.device_label = label
        sub.server = ntfy.server_url()
        sub.save(update_fields=["device_label", "server"])
        sub.record_seen()
        return JsonResponse(_serialize(sub), status=200)

    sub = Subscription.objects.create(
        user=request.user,
        topic=generate_topic(ntfy.topic_prefix()),
        server=ntfy.server_url(),
        device_label=label,
        last_seen_at=timezone.now(),
    )
    return JsonResponse(_serialize(sub), status=201)


# ------------------------------------------------------------------ E4 / E5
@require_http_methods(["PATCH", "DELETE"])
@api_login_required
@_json_endpoint
def subscription_detail(request, pk: int):
    """`/api/mobile/subscriptions/<id>/` —— 修改（PATCH）与吊销（DELETE）。"""
    if request.method == "DELETE":
        return _delete_subscription(request, pk)
    return _update_subscription(request, pk)


def _update_subscription(request, pk: int):
    sub = _owned_or_404(request.user, pk)
    data = _json_body(request)

    if "topic" in data:
        # topic 是安全边界：改了等于静默换频道（旧频道仍有人在听）。
        # 必须走「吊销 + 重新注册」，不能原地改。
        raise _ApiError(
            "validation_error", "topic 不可修改；请吊销该订阅后重新注册。"
        )

    fields: list[str] = []
    if "device_label" in data:
        sub.device_label = str(data.get("device_label") or "").strip()[:MAX_DEVICE_LABEL]
        fields.append("device_label")
    if "enabled" in data:
        sub.enabled = _as_bool(data.get("enabled"))
        fields.append("enabled")
    if "dnd_start" in data:
        sub.dnd_start = _parse_time(data.get("dnd_start"), "dnd_start")
        fields.append("dnd_start")
    if "dnd_end" in data:
        sub.dnd_end = _parse_time(data.get("dnd_end"), "dnd_end")
        fields.append("dnd_end")

    if fields:
        sub.save(update_fields=fields)
    return JsonResponse(_serialize(sub))


def _delete_subscription(request, pk: int):
    """吊销订阅（契约 §3.5）。

    服务端**不对 ntfy 做任何动作**：ntfy 没有"删除 topic"的授权模型
    （topic 是发布时即时创建的）。吊销的语义是「从本表移除 → 后端不再推送」。
    已经躺在 ntfy 缓存里的旧消息无法撤回 —— 决策 D11 已接受这一点。
    """
    sub = _owned_or_404(request.user, pk)
    sub.delete()
    return JsonResponse({"deleted": True})


# ------------------------------------------------------------------ E6
@require_POST
@api_login_required
@_json_endpoint
def subscription_test(request, pk: int):
    """发一条测试推送（契约 §3.6，决策 D6）。

    **同步发送**，不排队：这个端点的全部价值就是立刻告诉用户"通没通"，
    排队会把"推送不通"和"队列积压"两种故障混在一起，正好背离它存在的理由
    （`03-通知与推送设计.md` §7 要求把这两类故障彻底分开）。

    `mode` 固定为 `"direct"`，与通知派发路径的 `queued`/`inline` 区分开。

    注意：本端点**不受 `ntfy_enabled` 总开关限制**（只要求配了 server_url），
    这样在正式启用之前就能先验证链路。
    """
    sub = _owned_or_404(request.user, pk)
    result = ntfy.publish(
        sub.topic,
        title="OriDesk 测试推送",
        message=f"看到这条说明推送链路是通的（设备：{sub.device_label or '未命名'}）",
        tags="oridesk,test",
        sequence_id=f"test_{sub.pk}",
    )
    if result.ok:
        return JsonResponse({"sent": True, "mode": "direct"})
    return JsonResponse(
        {"sent": False, "mode": "direct", "error": result.error},
        status=200,
    )
