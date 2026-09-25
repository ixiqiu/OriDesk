"""防循环过滤 / 退信识别（开发文档 §6.1）。

对外接口（已冻结，pipeline 按此调用）：

- :func:`is_loop_mail` —— 命中任一条件即不生成工单、不触发自动回复。
- :func:`is_bounce_mail` —— 退信（DSN）识别。
- :func:`loop_reason` —— 返回命中的具体原因，便于日志与测试。

§6.1 的条件清单：

1. ``Auto-Submitted`` ∈ (auto-generated, auto-replied)
2. ``X-Auto-Response-Suppress`` == all
3. ``Precedence`` ∈ (bulk, junk, list)
4. ``List-Id`` 存在
5. ``From`` 命中本系统任意 ``Mailbox.email``
6. ``From`` 匹配 ``^(no-?reply|mailer-daemon|postmaster)@``
7. 另加退信识别（见 :func:`is_bounce_mail`）
"""

from __future__ import annotations

import re
from email.header import decode_header, make_header

from apps.core.utils import extract_email

#: loop_reason 可能返回的原因（便于日志检索与断言）。
REASON_AUTO_SUBMITTED = "auto_submitted"
REASON_AUTO_RESPONSE_SUPPRESS = "x_auto_response_suppress"
REASON_PRECEDENCE = "precedence"
REASON_LIST_ID = "list_id"
REASON_OWN_MAILBOX = "own_mailbox"
REASON_NOREPLY_SENDER = "noreply_sender"
REASON_BOUNCE = "bounce"

_AUTO_SUBMITTED_VALUES = ("auto-generated", "auto-replied")
_PRECEDENCE_VALUES = ("bulk", "junk", "list")
_NOREPLY_RE = re.compile(r"^(no-?reply|mailer-daemon|postmaster)@", re.I)
_MAILER_DAEMON_RE = re.compile(r"\bmailer-daemon\b", re.I)


def _header(msg, name: str) -> str:
    """读取邮件头并返回已解码、去首尾空白的字符串。"""
    if msg is None:
        return ""
    raw = msg.get(name, "")
    if raw is None:
        return ""
    value = str(raw)
    try:
        value = str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError, ValueError):
        pass
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _first_token(value: str) -> str:
    """取逗号/分号前的第一个取值，兼容 ``Precedence: bulk, list`` 之类写法。"""
    return re.split(r"[,;]", value or "", maxsplit=1)[0].strip().lower()


def is_bounce_mail(msg) -> bool:
    """判断是否为退信 / 投递状态通知（DSN，开发文档 §6.1 第 7 条）。

    命中任一条件：

    - ``Content-Type`` 为 ``multipart/report`` 且 ``report-type=delivery-status``；
    - ``From`` 含 ``MAILER-DAEMON``（大小写不敏感）；
    - ``Return-Path`` 为空地址 ``<>``（RFC 5321 的空反向路径，仅退信会这样）。

    :param msg: ``email.message.Message``。
    :return: 是退信返回 ``True``。
    """
    content_type = re.sub(r"\s+", "", _header(msg, "Content-Type").lower())
    if content_type.startswith("multipart/report") and (
        "report-type=delivery-status" in content_type
    ):
        return True

    from_header = _header(msg, "From")
    if from_header and _MAILER_DAEMON_RE.search(from_header):
        return True

    return _header(msg, "Return-Path").strip() == "<>"


def loop_reason(msg, *, mailbox_model=None) -> str | None:
    """返回命中的防循环原因，未命中返回 ``None``（开发文档 §6.1）。

    :param msg: ``email.message.Message``。
    :param mailbox_model: 用于「From 命中本系统邮箱」判断的模型，默认
        ``apps.accounts.models.Mailbox``（惰性导入，避免 app 未就绪）。
    :return: 命中的原因常量，例如 ``"auto_submitted"``。
    """
    # 1. Auto-Submitted
    auto_submitted = _header(msg, "Auto-Submitted").lower()
    if auto_submitted:
        token = _first_token(auto_submitted)
        if token in _AUTO_SUBMITTED_VALUES or token.startswith(
            ("auto-generated", "auto-replied")
        ):
            return REASON_AUTO_SUBMITTED

    # 2. X-Auto-Response-Suppress
    if _header(msg, "X-Auto-Response-Suppress").lower() == "all":
        return REASON_AUTO_RESPONSE_SUPPRESS

    # 3. Precedence
    if _first_token(_header(msg, "Precedence")) in _PRECEDENCE_VALUES:
        return REASON_PRECEDENCE

    # 4. List-Id
    if _header(msg, "List-Id"):
        return REASON_LIST_ID

    sender = extract_email(_header(msg, "From"))

    # 5. From 命中本系统任意 Mailbox.email
    if sender and "@" in sender:
        model = mailbox_model
        if model is None:
            from apps.accounts.models import Mailbox

            model = Mailbox
        if model.objects.filter(email__iexact=sender).exists():
            return REASON_OWN_MAILBOX

    # 6. noreply / mailer-daemon / postmaster
    if sender and _NOREPLY_RE.match(sender):
        return REASON_NOREPLY_SENDER

    # 7. 退信
    if is_bounce_mail(msg):
        return REASON_BOUNCE

    return None


def is_loop_mail(msg, *, mailbox_model=None) -> bool:
    """是否为应丢弃的循环邮件（开发文档 §6.1）。

    命中任一条件即：

    - 不生成工单
    - 不触发自动回复

    :param msg: ``email.message.Message``。
    :param mailbox_model: 见 :func:`loop_reason`。
    :return: 命中任一条件返回 ``True``。
    """
    return loop_reason(msg, mailbox_model=mailbox_model) is not None
