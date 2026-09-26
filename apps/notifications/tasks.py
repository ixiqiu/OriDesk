"""通知的异步派发（契约 §4.4：**必须异步，不阻塞收信流水线**）。

为什么走队列：`notify_event` 要对每个收件人的**每台设备**发一次 HTTP（5s 超时）。
若在收信流水线里同步做，一次消息涌入几十封时会把 scheduler 进程卡住，
IMAP 轮询整体推迟 —— 这正是契约 §4.4 说的"不做就是噪音炸弹"之外的第二个坑。

队列选择：**`default`，不是 `mail`**。`mail` 队列上跑的是真实发信（SMTP 可能很慢），
通知没必要排在它后面；而 `docker-compose.yml` 里 worker 的命令是
`rqworker default mail`，`default` 已有 worker 监听，**不要**新造队列名
（新队列名会导致任务永远没人消费，且这种故障是静默的）。

Redis 不可用时，`apps.mailboxes.tasks.dispatch()` 会**同步降级**并告警 —— 这是既有机制，
直接复用，不自己判断 Redis 可用性。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

NOTIFY_QUEUE = "default"


def notify_event_task(
    event: str,
    ticket_id: int,
    actor_id: int | None = None,
    mentioned_ids: list[int] | None = None,
    subject: str = "",
) -> dict:
    """RQ 任务：按 id 取对象再派发。

    只传 id 不传对象：RQ 会把参数序列化进 Redis，Django 模型实例不适合直接入队。
    """
    from django.contrib.auth import get_user_model

    from apps.notifications.services import notify_event
    from apps.tickets.models import Ticket

    ticket = Ticket.objects.filter(pk=ticket_id).select_related("group", "assignee").first()
    if ticket is None:
        return {}

    User = get_user_model()
    actor = User.objects.filter(pk=actor_id).first() if actor_id else None
    mentioned = (
        list(User.objects.filter(pk__in=mentioned_ids)) if mentioned_ids else []
    )
    return notify_event(
        event, ticket, actor=actor, mentioned=mentioned, subject=subject
    )


def enqueue_notify(
    event: str,
    ticket,
    *,
    actor=None,
    mentioned=None,
    subject: str = "",
) -> str:
    """把通知放进队列。

    返回值：`"queued"`（已入 RQ）/ `"inline"`（Redis 不可用，已同步执行）/
    `"deferred"`（当前处于事务中，已挂到 `on_commit`，提交后才真正派发）/
    `"failed"`（入队本身出错，已吞掉）。

    **为什么必须区分 `deferred`（重要）**：`apps.tickets.services.add_note` 带
    `@transaction.atomic`。若在事务提交前就把任务交给 RQ，worker 可能立刻开始执行，
    此时它读到的是**未提交**的数据库状态（其他连接看不到这些行），
    通知内容与实际入库结果就可能不一致。正确做法是 `transaction.on_commit`：
    提交后才入队。而不在事务中时（收信流水线、改派）它等价于立即执行，没有额外延迟。

    **本函数不抛异常**：它挂在收信主链路上，任何意外都不能让一封信丢掉。
    """
    from django.db import transaction

    def _dispatch() -> str:
        from apps.mailboxes.tasks import dispatch

        mode, _ = dispatch(
            notify_event_task,
            event,
            ticket.pk,
            getattr(actor, "pk", None),
            [u.pk for u in (mentioned or [])],
            subject,
            queue=NOTIFY_QUEUE,
        )
        return mode

    try:
        if transaction.get_connection().in_atomic_block:
            transaction.on_commit(_dispatch)
            return "deferred"
        return _dispatch()
    except Exception:  # noqa: BLE001 - 通知派发失败绝不能影响主流程
        logger.exception("通知任务入队失败（已忽略，不影响主流程）。")
        return "failed"
