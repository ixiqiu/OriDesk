"""内部备注里的 `@username` 提及解析（契约 §4.4，源自 `03-通知与推送设计.md` §3）。

这套规则要同时满足两件互相拉扯的事：

1. **不能让邮箱地址里的 `@` 变成提及。** `add_note` 的正文里出现
   `客户邮箱是 a@b.com` 是极常见的，若把 `b.com` 当成用户名，就等于凭空造出
   一条提及。契约明确要求「`@` 前面不能是字母数字」，并且**必须写测试覆盖**。
2. **中文用户名要能用。** 本项目是中文系统，用户名可能是 `张三`。若只认 ASCII，
   `@张三` 会被静默丢弃 —— 用户以为提到了，实际没人收到，这种失败最难排查。

取舍如下：

- 用户名部分用 Unicode 的 ``\\w``，因此 `@张三` 能匹配。
- 代价是「`请@张三看一下`」这种**没有分隔符**的写法会把 `张三看一下` 整体捕获。
  这里是**故意选择让它多匹配**：捕获结果随后要用 `username__iexact` 精确查库，
  多匹配只会查不到人（fail closed，不通知），不会误通知；而少匹配则会静默丢功能。
  两端都不是安全漏洞，所以选失败方向更安全的那边。
- 右侧结尾的 `.` `-` `+` 会被剥掉，否则 `请 @tech1.` 会得到用户名 `tech1.`
  （句号是句末标点，不是用户名的一部分）。

**权限校验（这是安全边界，不能省）**：被 @ 的人必须**本来就能看见这张工单**。
没有这道校验，`@` 就成了「给任意人推送任意工单摘要」的越权通告渠道
（`03-通知与推送设计.md` §3 原话）。
"""

from __future__ import annotations

import re

# 左侧否定环视**只挡 ASCII 的字母数字与 `_ . + -`**，刻意不用 `\w`：
# `\w` 在 Unicode 下会把中文也算进去，于是「请@张三」这种写法会因为 `@` 前面是
# 汉字而被误判成邮箱场景、静默丢弃。而邮箱 local part 实际上不会出现汉字，
# 所以环视用 ASCII 类、捕获组用 Unicode 的 `\w`，两边各取所需。
MENTION_RE = re.compile(r"(?<![0-9A-Za-z_.+\-])@(\w[\w.+\-]*)", re.UNICODE)

_TRAILING_PUNCTUATION = ".-+"


def extract_mention_names(text: str | None) -> list[str]:
    """从纯文本里抽出被提及的用户名（去重、保持出现顺序、大小写不敏感）。

    只做**文本解析**，不碰数据库；权限校验在 `resolve_mentions`。
    """
    seen: set[str] = set()
    names: list[str] = []
    for match in MENTION_RE.finditer(text or ""):
        name = match.group(1).rstrip(_TRAILING_PUNCTUATION)
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def resolve_mentions(ticket, text: str | None, *, exclude_user=None) -> list:
    """把正文里的 `@username` 解析成**有权看见该工单**的用户列表。

    - 用户名精确匹配、不区分大小写（不匹配显示名，避免重名歧义）。
    - 被 @ 者必须通过可见性校验（`visible_tickets`），否则静默忽略。
    - `exclude_user`（通常是备注作者）不出现在结果里：自己 @ 自己不该收到推送。
    """
    from django.contrib.auth import get_user_model

    from apps.tickets.selectors import visible_tickets

    names = extract_mention_names(text)
    if not names:
        return []

    User = get_user_model()
    found: dict[str, object] = {}
    for name in names:
        user = User.objects.filter(username__iexact=name).first()
        if user is None:
            continue
        if exclude_user is not None and user.pk == getattr(exclude_user, "pk", None):
            continue
        # 安全边界：不可见这张工单的人，不能被 @ 通知（否则等于越权通告渠道）。
        if not visible_tickets(user).filter(pk=ticket.pk).exists():
            continue
        found[name.casefold()] = user

    # 按 names 的顺序取回，保证同一份输入产生稳定顺序（便于测试与聚合去重）。
    ordered: list = []
    for name in names:
        user = found.pop(name.casefold(), None)
        if user is not None:
            ordered.append(user)
    return ordered
