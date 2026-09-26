"""IMAP 连接工厂（开发文档 §6.4）。

把"连接 → 加密 → 登录"这段逻辑集中到一处，供两个调用方复用：
- `apps/mailboxes/sync.py`：定时增量同步；
- 账号管理界面的"连通性检查"按钮（`apps/accounts/views.py::_imap_inbox_count`）。

**加密策略**（模型里只有 `imap_ssl` 一个开关，用它表达两种安全连接，无需新增字段）：

| `imap_ssl` | 连接方式 | 常用端口 |
|---|---|---|
| `True` | 隐式 TLS：连接即加密 | 993 |
| `False` | 明文连接后，只要服务器声明支持 STARTTLS 就**升级为加密**再登录 | 143 |

`imap_ssl=False` 且服务器不支持 STARTTLS 时会**直接报错**，不会退回明文登录——
避免邮箱授权码以明文过网。这条规则对任何服务商一致，与是否为飞书无关。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class MailSyncError(Exception):
    """IMAP 同步失败。"""


class ImapConnectionError(MailSyncError):
    """IMAP 连接/加密/登录失败（消息可直接展示给管理员）。

    继承 `MailSyncError`：同步流程里"连接失败"与"处理失败"对调用方是同一类错误，
    调度器只需捕获一个异常类型。
    """


def starttls_supported(client) -> bool:
    """服务器是否声明支持 STARTTLS。"""
    try:
        return bool(client.has_capability("STARTTLS"))
    except Exception:  # noqa: BLE001 - 能力查询失败时按不支持处理（更保守）
        try:
            capabilities = client.capabilities()
        except Exception:  # noqa: BLE001
            return False
        return any(str(item).upper() == "STARTTLS" for item in capabilities or ())


# 客户端标识：部分服务商（网易 163/126/yeah 个人邮箱所在的 Coremail 平台）
# 要求第三方客户端在登录后按 RFC 2971 发送 ID 命令，否则后续 SELECT 会被拒绝：
#   "select failed: SELECT Unsafe Login. Please contact kefu@188.com for help"
# 这里声明本系统的身份；不声明身份的裸登录会被网易判定为"不安全登录"。
CLIENT_ID_NAME = "OriDesk"
CLIENT_ID_VERSION = "1.1"
CLIENT_ID_VENDOR = "OriDesk"


def send_client_id(client, mailbox) -> None:
    """按 RFC 2971 发送 IMAP ID 客户端标识（服务商不要求时静默跳过）。

    设计取向：**绝不因为这条命令影响连接本身**。
    - 服务器未声明 `ID` 能力 → 直接跳过（imapclient 的 `id_()` 有
      `@require_capability("ID")`，硬发会抛错）；
    - 发送过程任何异常只记 warning，不影响已建立的登录会话。
    """
    try:
        if not client.has_capability("ID"):
            return
    except Exception:  # noqa: BLE001 - 能力查询失败时按"不支持"处理（更保守）
        return

    try:
        client.id_(
            {
                "name": CLIENT_ID_NAME,
                "version": CLIENT_ID_VERSION,
                "vendor": CLIENT_ID_VENDOR,
            }
        )
        logger.info("IMAP %s 已发送客户端 ID 标识。", mailbox.imap_host)
    except Exception as exc:  # noqa: BLE001 - ID 失败不应阻断连接
        logger.warning("IMAP %s 发送 ID 失败（已忽略）：%s", mailbox.imap_host, exc)


def connect_imap(mailbox, secret: str, *, timeout: int = 60):
    """建立**已登录**的 IMAP 连接，失败抛 `ImapConnectionError`。

    返回值是 `imapclient.IMAPClient` 实例（支持 `with` 使用，用完自动 logout）。
    """
    from imapclient import IMAPClient

    try:
        client = IMAPClient(
            mailbox.imap_host,
            port=mailbox.imap_port,
            ssl=mailbox.imap_ssl,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 网络/证书/端口错误统一包装
        raise ImapConnectionError(
            f"无法连接 IMAP 服务器 {mailbox.imap_host}:{mailbox.imap_port}：{exc}"
        ) from exc

    try:
        if not mailbox.imap_ssl:
            if not starttls_supported(client):
                raise ImapConnectionError(
                    f"IMAP 服务器 {mailbox.imap_host}:{mailbox.imap_port} 未提供 STARTTLS；"
                    "出于安全考虑不会以明文提交密码。请改用 SSL 端口（如 993）或更换服务商配置。"
                )
            client.starttls()
            logger.info("IMAP %s 已通过 STARTTLS 升级为加密连接。", mailbox.imap_host)
        client.login(mailbox.username, secret)
    except ImapConnectionError:
        _safe_close(client)
        raise
    except Exception as exc:  # noqa: BLE001 - 认证失败/服务端拒绝
        _safe_close(client)
        raise ImapConnectionError(
            f"IMAP 登录失败（{mailbox.username}@{mailbox.imap_host}）：{exc}"
        ) from exc

    # 登录成功后再声明客户端身份（网易等 Coremail 平台要求，否则 SELECT 会被拒）
    send_client_id(client, mailbox)
    return client


def _safe_close(client) -> None:
    try:
        client.logout()
    except Exception:  # noqa: BLE001 - 关闭失败不影响错误上报
        pass
