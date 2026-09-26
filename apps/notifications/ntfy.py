"""ntfy 发布客户端（契约 §4.1）。

**零新增依赖**（契约 §1.4）：用 stdlib `urllib.request`，不引 requests/httpx
（已核对 requirements.txt，两者都不在）。

三条硬性要求，都来自契约与既有代码的防御风格：

1. **必须设超时**，否则一个卡住的 ntfy 会拖死 scheduler 进程的收信循环。
2. **绝不抛异常**。推送失败不能影响收信流水线（`02-OriDesk后端侦察.md` §2.1）。
   所有失败都收敛成 `PublishResult(ok=False, ...)` 并记日志。
3. **日志里永不出现 token 明文**，用 `apps.core.crypto.mask_secret`。

发布协议（已核 ntfy 官方文档 `docs.ntfy.sh/publish/`）：

    POST {server}/{topic}
    Authorization: Bearer {token}     ← 仅当配置了 token
    Title / Priority / Tags / Click / Actions / X-Sequence-ID
    Content-Type: text/plain; charset=utf-8

    <消息正文>
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib import error, request

from apps.audit.models import Setting
from apps.core.crypto import CredentialError, decrypt_secret, encrypt_secret, mask_secret

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5
"""秒。契约 §4.1 建议值。"""

MAX_TITLE = 150
MAX_BODY = 120
"""正文只放工单主题，契约 §4.3 要求截断，避免超长主题撑爆通知。"""

SETTING_ENABLED = "ntfy_enabled"
SETTING_SERVER = "ntfy_server_url"
SETTING_TOKEN = "ntfy_token"
SETTING_PREFIX = "ntfy_topic_prefix"
SETTING_AGGREGATE_SECONDS = "notify_aggregate_seconds"
SETTING_PUBLIC_BASE_URL = "mobile_public_base_url"


@dataclass
class PublishResult:
    """发布结果。`ok=False` 时 `error` 一定有值，`status` 可能为 None（未连上）。"""

    ok: bool
    status: int | None = None
    error: str = ""


# ------------------------------------------------------------------ 配置读取
def server_url() -> str:
    return (Setting.get(SETTING_SERVER) or "").strip().rstrip("/")


def is_enabled() -> bool:
    """总开关。默认关（未配置就不该发，避免装完就报错）。"""
    if not Setting.get_bool(SETTING_ENABLED, False):
        return False
    return bool(server_url())


def topic_prefix() -> str:
    from apps.notifications.models import DEFAULT_TOPIC_PREFIX

    return (Setting.get(SETTING_PREFIX) or DEFAULT_TOPIC_PREFIX).strip()


def aggregate_seconds() -> int:
    value = Setting.get_int(SETTING_AGGREGATE_SECONDS, 60)
    # 窗口必须为正，否则聚合键会退化成"每纳秒一个桶"，比不聚合还糟。
    return value if value and value > 0 else 60


def public_base_url() -> str:
    return (Setting.get(SETTING_PUBLIC_BASE_URL) or "").strip().rstrip("/")


def get_token() -> str:
    """读取并解密 ntfy 访问令牌。未配置或解密失败都返回空串（= 实例不鉴权）。"""
    raw = (Setting.get(SETTING_TOKEN) or "").strip()
    if not raw:
        return ""
    try:
        # 决策 D4：token 以 Fernet 密文存放，明文字节 -> 密文字节 -> base64 文本入库。
        return decrypt_secret(raw.encode("utf-8"))
    except CredentialError:
        logger.error(
            "ntfy_token 解密失败（FERNET_KEY 可能已更换）：本次推送按「无令牌」处理。"
        )
        return ""


def set_token(plaintext: str | None, *, user=None) -> None:
    """写入 ntfy 令牌（加密后存 Setting）。空值表示清除。"""
    text = (plaintext or "").strip()
    stored = encrypt_secret(text).decode("utf-8") if text else ""
    Setting.set(SETTING_TOKEN, stored, user=user)


# ------------------------------------------------------------------ 发布
def publish(
    topic: str,
    *,
    title: str,
    message: str,
    click: str = "",
    actions: str = "",
    priority: str = "default",
    tags: str = "oridesk",
    sequence_id: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> PublishResult:
    """向一个 topic 发布（或更新）一条通知。**任何情况下都不抛异常。**"""
    base = server_url()
    if not base:
        return PublishResult(ok=False, error="ntfy_server_url 未配置")
    if not topic:
        return PublishResult(ok=False, error="topic 为空")

    url = f"{base}/{topic}"
    headers = {
        "Title": title[:MAX_TITLE],
        "Priority": priority,
        "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if click:
        headers["Click"] = click
    if actions:
        headers["Actions"] = actions
    if sequence_id:
        # 契约 §4.4 的聚合靠它：同一 sequence 的后续消息在客户端**替换**前一条，
        # 而不是再堆一条。这正是"60s 窗口内合并为一条"的实现手段，
        # 免掉了延时任务（见 services.py 的说明）。
        headers["X-Sequence-ID"] = sequence_id

    token = get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = message[:MAX_BODY].encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method="POST")

    try:
        with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 地址来自受信设置项
            status = resp.status
            resp.read()
        if 200 <= status < 300:
            return PublishResult(ok=True, status=status)
        return PublishResult(ok=False, status=status, error=f"HTTP {status}")
    except error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 - 读取错误响应体失败不影响主流程
            pass
        logger.warning(
            "ntfy 发布失败：HTTP %s（topic=%s，token=%s）%s",
            exc.code,
            topic[:12],
            mask_secret(token) if token else "无",
            detail,
        )
        return PublishResult(ok=False, status=exc.code, error=f"HTTP {exc.code} {detail}".strip())
    except Exception as exc:  # noqa: BLE001 - 网络类异常全部收敛，绝不冒泡
        logger.warning(
            "ntfy 发布异常（topic=%s，token=%s）：%s",
            topic[:12],
            mask_secret(token) if token else "无",
            exc,
        )
        return PublishResult(ok=False, error=str(exc))


def health_check(timeout: int = DEFAULT_TIMEOUT) -> dict:
    """探活：GET {server}/v1/health（ntfy 官方健康端点）。

    失败不算错误，返回结构给设置页/运维排查用。
    """
    base = server_url()
    if not base:
        return {"ok": False, "error": "ntfy_server_url 未配置"}
    try:
        req = request.Request(f"{base}/v1/health", method="GET")
        token = get_token()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        return {"ok": bool(payload.get("healthy")), "detail": payload}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
