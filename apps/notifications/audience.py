"""事件 → 受众解析（契约 §4.4，规则源自 `03-通知与推送设计.md` §1）。

四条事件与受众：

| 事件 | 触发点 | 受众 |
|---|---|---|
| 新工单 | `process_inbound` 返回 `created=True` | 该组全体成员 |
| 客户新来信 | `process_inbound`，`created=False` | ① 认领人；② **未认领时 → 该组全体** |
| 改派到本组 | `reassign_ticket()` 成功后 | **新组**全体成员（原组已失可见） |
| 内部备注 | `add_note()` 后 | ① 被 @ 者；② 认领人 |

三条全局规则（**所有事件都必须过这三关**）：

1. 不通知动作发起人自己（你回复完不该收到自己的通知）。
2. 同一个人不重复推（组广播与认领人重合时只推一条）。
3. `status="closed"` 的工单不推。

受众语义的用户原话是「未认领全推，认领之后只推给认领的人」，与开发文档 §2.6
「认领后只有认领人看到待回复」是同一套所有权模型 —— 用户不必学两套规则。

**已知运维后果（`01-决策记录.md` 要求记录）**：认领人休假 / 离职 / 就是不看手机时，
该工单对全组静默。`assignee` 是 `SET_NULL`，用户被删除会自动退回全组广播
（「人不在系统里」有兜底），**「人在但不看」没有**。用户明确选择不加超时推送。
"""

from __future__ import annotations

EVENT_TICKET_CREATED = "ticket_created"
EVENT_TICKET_INBOUND = "ticket_inbound"
EVENT_TICKET_REASSIGNED = "ticket_reassigned"
EVENT_NOTE_MENTIONED = "note_mentioned"

ALL_EVENTS = (
    EVENT_TICKET_CREATED,
    EVENT_TICKET_INBOUND,
    EVENT_TICKET_REASSIGNED,
    EVENT_NOTE_MENTIONED,
)


def group_members(group):
    """组内全体有效成员。

    过滤 `is_active=False`：停用账号不该再收到推送（它们的订阅通常也早已吊销）。
    用 `distinct()` 是因为 `user_groups` 是反向外键连接，理论上不会重复，
    但显式去重比依赖"理论上"更便宜。
    """
    from django.contrib.auth import get_user_model

    if group is None:
        return []
    User = get_user_model()
    return list(
        User.objects.filter(user_groups__group=group, is_active=True)
        .distinct()
        .order_by("pk")
    )


def resolve_recipients(event: str, ticket, *, actor=None, mentioned=None) -> list:
    """算出某事件该推给谁。返回去重后的用户列表（保持稳定顺序）。"""
    if event not in ALL_EVENTS:
        raise ValueError(f"未知的通知事件：{event}")

    # 规则 3：已关闭工单不推。放在最前面，省掉后面所有查询。
    if getattr(ticket, "status", "") == "closed":
        return []

    if event in (EVENT_TICKET_CREATED, EVENT_TICKET_REASSIGNED):
        candidates = group_members(ticket.group)
    elif event == EVENT_TICKET_INBOUND:
        # 认领后只推认领人；未认领则全组。
        assignee = ticket.assignee if ticket.assignee_id else None
        candidates = [assignee] if assignee is not None else group_members(ticket.group)
    else:  # EVENT_NOTE_MENTIONED
        candidates = list(mentioned or [])
        if ticket.assignee_id and ticket.assignee is not None:
            candidates.append(ticket.assignee)

    # 规则 1 + 2：排除发起人、去重、过滤停用账号。
    actor_pk = getattr(actor, "pk", None)
    seen: set[int] = set()
    recipients: list = []
    for user in candidates:
        if user is None:
            continue
        if actor_pk is not None and user.pk == actor_pk:
            continue
        if not getattr(user, "is_active", True):
            continue
        if user.pk in seen:
            continue
        seen.add(user.pk)
        recipients.append(user)
    return recipients
