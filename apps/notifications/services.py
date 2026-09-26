"""通知编排：受众解析 → 聚合 → 发布（契约 §4.4）。

**聚合为什么不用延时任务**（重要设计决定）

契约 §4.4 要求「60s 窗口内同一工单多事件合并为一条，正文改成『T#123 收到 3 封新来信』」。
要做到"一条里写着 3"，直觉方案是把发布推迟到窗口关闭再执行，但那需要延时任务，
而既有的 `apps.mailboxes.tasks.dispatch()` **不支持延时**（它只做 `enqueue` 或同步降级）。

改用 ntfy 原生的**通知更新**机制（已核官方文档："To update an existing notification,
publish a new message with the same sequence ID. Clients will replace the previous
notification with the new one."）：

- 每个 (收件人, 聚合桶) 对应**一个** `X-Sequence-ID`；
- 窗口内每次事件都带着**同一个** sequence id 重新发布，客户端把旧的那条**替换**掉。

效果与"延时到窗口结束再发一条"完全一致（用户在通知栏里始终只看到一条，数字递增），
却不需要延时任务、不需要额外进程，且**任何一条都不会丢**——即使消息只来了一封，
它也已经发出去了，不存在"等窗口时进程重启导致丢失"的窗口期。

**LocMemCache 的前提（必须知道）**：聚合状态放在 Django cache 里，而本项目的
`CACHES` 固定是 `LocMemCache`（**每进程一份**）。这不影响正确性，因为通知一律经
`tasks.enqueue_notify` 进 RQ，而 `docker-compose.yml` 里**只有一个 worker 容器**，
聚合计数因此集中在该 worker 进程内，跨事件源（收信 / 改派 / 备注）也是自洽的。
唯一例外是 Redis 不可用时的**同步降级**：此时事件在各自调用进程内聚合，
最坏后果是**少聚合**（多发一两条重复通知），绝不会漏发。这是刻意接受的取舍。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.core.cache import cache

from apps.notifications import ntfy
from apps.notifications.audience import (
    EVENT_NOTE_MENTIONED,
    EVENT_TICKET_CREATED,
    EVENT_TICKET_INBOUND,
    EVENT_TICKET_REASSIGNED,
    resolve_recipients,
)

logger = logging.getLogger(__name__)

AGG_CACHE_PREFIX = "notifications:agg:"

TAG_BY_EVENT = {
    EVENT_TICKET_CREATED: "oridesk,new",
    EVENT_TICKET_INBOUND: "oridesk,inbox",
    EVENT_TICKET_REASSIGNED: "oridesk,reassign",
    EVENT_NOTE_MENTIONED: "oridesk,mention",
}


@dataclass
class _Bucket:
    """一个聚合桶的内容（序列化成 JSON 存 cache）。"""

    events: list[dict] = field(default_factory=list)

    def add(self, event: str, ticket_id: int, subject: str) -> None:
        self.events.append({"e": event, "t": ticket_id, "s": subject})

    @property
    def ticket_ids(self) -> list[int]:
        seen: list[int] = []
        for item in self.events:
            if item["t"] not in seen:
                seen.append(item["t"])
        return seen


def _bucket_key(user_id: int, bucket: int) -> str:
    return f"{AGG_CACHE_PREFIX}{user_id}:{bucket}"


def _load_bucket(user_id: int, bucket: int) -> _Bucket:
    raw = cache.get(_bucket_key(user_id, bucket))
    if not isinstance(raw, list):
        return _Bucket()
    return _Bucket(events=[item for item in raw if isinstance(item, dict)])


def _save_bucket(user_id: int, bucket: int, data: _Bucket, ttl: int) -> None:
    # TTL 留 2 倍窗口：桶键本身按窗口编号生成，多活一会儿只是防边界抖动，
    # 不会让两个窗口的内容串到一起（键里含 bucket 号）。
    cache.set(_bucket_key(user_id, bucket), data.events, max(ttl * 2, 60))


# ------------------------------------------------------------------ 文案
def _compose(event: str, bucket: _Bucket, ticket, subject: str = "") -> tuple[str, str]:
    """生成 (title, message)。

    **推送内容只放工单号与主题，绝不放邮件正文**（契约 §4.3：邮件正文是客户隐私，
    推送要经过服务器，即使是自建也不外泄）。所以这里可以出现 `subject`，但绝不能出现
    `Message.body_text`。
    """
    subject = (subject or getattr(ticket, "subject", "") or "").strip()
    count = len(bucket.events)
    ticket_ids = bucket.ticket_ids
    prefix = f"T#{ticket.pk}" if ticket is not None else ""

    if count <= 1:
        titles = {
            EVENT_TICKET_CREATED: f"{prefix} 新工单",
            EVENT_TICKET_INBOUND: f"{prefix} 有新来信",
            EVENT_TICKET_REASSIGNED: f"{prefix} 已改派到本组",
            EVENT_NOTE_MENTIONED: f"{prefix} 有人提到了你",
        }
        return titles.get(event, f"{prefix} 有更新"), subject

    # 窗口内多个事件：收敛成一条摘要，避免"噪音炸弹"（契约 §4.4）。
    if len(ticket_ids) == 1:
        if event == EVENT_TICKET_INBOUND:
            return f"{prefix} 收到 {count} 封新来信", subject
        return f"{prefix} 有 {count} 条新动态", subject

    # 多个工单（典型场景：一次 IMAP 轮询拉回一堆新单）。
    if event == EVENT_TICKET_CREATED:
        return f"组内新增 {len(ticket_ids)} 个工单", subject
    return f"有 {len(ticket_ids)} 个工单有新动态", subject


def _click_targets(ticket) -> tuple[str, str]:
    """返回 (click, actions)。

    决策 D3：**scheme 优先**，让点击直接唤起我们自己的 App、跳进 WebView 到工单页，
    不绕浏览器（绕浏览器要重新登录一次）。但 ntfy 的 `Click` 只能放一个 URL，
    方案 B 下我们无法为运行时才填的域名声明 Android App Link（清单里的 host 必须是
    编译期常量，与决策 10"地址不硬编码"冲突），所以 scheme 唤起失败时没有天然兜底。
    因此：`Click` 给 scheme，另挂一个 "在浏览器打开" 的 action 按钮作为兜底。
    """
    if ticket is None:
        return "", ""
    base = ntfy.public_base_url()
    web = f"{base}/tickets/{ticket.pk}/" if base else ""
    click = f"oridesk://ticket/{ticket.pk}"
    actions = f"view, 在浏览器打开, {web}, clear=true" if web else ""
    return click, actions


# ------------------------------------------------------------------ 主入口
def notify_event(
    event: str,
    ticket,
    *,
    actor=None,
    mentioned=None,
    subject: str = "",
    now=None,
) -> dict:
    """派发一条通知事件。**绝不抛异常**（调用方在收信主链路上）。

    返回统计字典，便于测试与排查：{'enabled', 'recipients', 'published', 'failed'}。
    """
    stats = {"enabled": False, "recipients": 0, "devices": 0, "published": 0, "failed": 0}
    try:
        if ticket is None:
            return stats
        if not ntfy.is_enabled():
            return stats
        stats["enabled"] = True

        recipients = resolve_recipients(
            event, ticket, actor=actor, mentioned=mentioned
        )
        stats["recipients"] = len(recipients)
        if not recipients:
            return stats

        from django.utils import timezone

        moment = now or timezone.now()
        window = ntfy.aggregate_seconds()
        bucket_no = int(moment.timestamp() // window)
        # sequence id 必须落在 ntfy 允许的字符集内：下划线合法，冒号不合法。
        sequence_id = f"agg_{bucket_no}"

        click, actions = _click_targets(ticket)
        title_hint = ""
        device_count = 0
        for user in recipients:
            data = _load_bucket(user.pk, bucket_no)
            data.add(event, ticket.pk, (subject or getattr(ticket, "subject", ""))[:120])
            _save_bucket(user.pk, bucket_no, data, window)

            title, message = _compose(event, data, ticket, subject=subject)
            title_hint = title
            # 契约 §3.3：**每用户每设备一个 topic**，所以必须逐设备发布，
            # 只发第一个会让用户的其他手机收不到。
            for topic in _topics_for(user):
                device_count += 1
                result = ntfy.publish(
                    topic,
                    title=title,
                    message=message,
                    click=click,
                    actions=actions,
                    priority="default",
                    tags=TAG_BY_EVENT.get(event, "oridesk"),
                    sequence_id=sequence_id,
                )
                if result.ok:
                    stats["published"] += 1
                else:
                    stats["failed"] += 1
        stats["devices"] = device_count
        logger.info(
            "通知事件 %s 于 T#%s：受众 %s 人 / %s 台设备，发布成功 %s，失败 %s（%s）",
            event,
            getattr(ticket, "pk", "?"),
            stats["recipients"],
            device_count,
            stats["published"],
            stats["failed"],
            title_hint,
        )
    except Exception:  # noqa: BLE001 - 通知失败绝不能影响收信
        logger.exception("通知事件 %s 派发失败（已忽略，不影响主流程）。", event)
    return stats


def _topics_for(user) -> list[str]:
    """该用户**全部启用设备**的 topic。

    上限 8 是防御性的：正常用户不会有 8 台设备，但一个失控的注册循环不该
    让一次收信去发上百个 HTTP 请求、把 scheduler 拖住。
    """
    return list(
        user.push_subscriptions.filter(enabled=True).values_list("topic", flat=True)[:8]
    )


def publish_to_user(user, *, title: str, message: str, sequence_id: str = "") -> int:
    """给某用户的**每一台启用设备**发同一条通知，返回成功条数。"""
    topics = list(
        user.push_subscriptions.filter(enabled=True).values_list("topic", flat=True)[:8]
    )
    click, actions = "", ""
    sent = 0
    for topic in topics:
        if ntfy.publish(
            topic,
            title=title,
            message=message,
            click=click,
            actions=actions,
            sequence_id=sequence_id,
        ).ok:
            sent += 1
    return sent
