"""`@username` 提及解析测试（契约 §4.4 / `03-通知与推送设计.md` §3）。

契约点名要求覆盖「`a@b.com` 里的 `@` 不能被当成提及」这个用例 —— 见 `TestEmailTrap`。
除此之外还覆盖中文用户名、结尾标点，以及**权限校验**（防越权通告渠道）。
"""

from __future__ import annotations

import pytest

from apps.notifications.mentions import extract_mention_names, resolve_mentions
from tests.conftest import make_ticket


class TestEmailTrap:
    """契约硬性要求：正文里的邮箱地址不能被误判成提及。"""

    @pytest.mark.parametrize(
        "text",
        [
            "客户邮箱是 a@b.com",
            "联系 user_name@example.com 处理",
            "请发到 first.last@example.com",
            "tag+label@example.com 这个也能收",
            "no-reply@example.com 是系统地址",
            "x-y@z.com",
        ],
    )
    def test_email_is_not_a_mention(self, text):
        assert extract_mention_names(text) == []

    def test_email_alongside_real_mention(self):
        """同一句里邮箱要被忽略、真正的提及要被抓到（不能因噎废食）。"""
        assert extract_mention_names("a@b.com 和 @tech1 都提到") == ["tech1"]
        assert extract_mention_names("发到 user_name@example.com，并 @tech1 知悉") == ["tech1"]


class TestExtraction:
    def test_plain_mention(self):
        assert extract_mention_names("@tech1") == ["tech1"]

    def test_mention_in_sentence(self):
        assert extract_mention_names("请 @tech1 看一下") == ["tech1"]

    def test_mention_in_parentheses(self):
        assert extract_mention_names("(@tech1)") == ["tech1"]

    def test_mention_after_chinese_without_space(self):
        """中文系统里 `请@张三` 是自然写法，不能被丢弃。

        回归用例：左侧否定环视若用 `\\w`（Unicode 语义），`@` 前面是汉字就会被
        误判成邮箱场景而静默丢弃。环视必须只挡 ASCII 邮箱字符。
        """
        assert extract_mention_names("请@张三 看看") == ["张三"]
        assert extract_mention_names("你好@tech1") == ["tech1"]

    def test_trailing_sentence_punctuation_stripped(self):
        """`请 @tech1. 谢谢` 里的句号是句末标点，不是用户名的一部分。"""
        assert extract_mention_names("麻烦 @tech1. 谢谢") == ["tech1"]
        assert extract_mention_names("找 @tech1, 或者 @tech2。") == ["tech1", "tech2"]

    def test_dedupe_case_insensitive_keeps_order(self):
        assert extract_mention_names("@TECH1 和 @tech1 和 @Tech2") == ["TECH1", "Tech2"]

    def test_no_mentions(self):
        assert extract_mention_names("这是一段没有提及的正文。") == []

    def test_empty_and_none(self):
        assert extract_mention_names("") == []
        assert extract_mention_names(None) == []

    def test_bare_at_sign_is_ignored(self):
        assert extract_mention_names("价格 @ 100 元") == []


class TestResolveMentions:
    def test_resolves_visible_user(self, tech_group, tech_user, tech_mailbox):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        got = resolve_mentions(ticket, "请 @tech1 看一下")
        assert [u.pk for u in got] == [tech_user.pk]

    def test_case_insensitive(self, tech_group, tech_user, tech_mailbox):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert [u.pk for u in resolve_mentions(ticket, "@TECH1 hi")] == [tech_user.pk]

    def test_author_is_excluded(self, tech_group, tech_user, tech_mailbox):
        """自己 @ 自己不该收到推送。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert resolve_mentions(ticket, "@tech1 自言自语", exclude_user=tech_user) == []

    def test_invisible_user_is_rejected(self, tech_group, finance_user, tech_mailbox):
        """**安全边界**：看不见这张工单的人不能被 @ 通知。

        没有这道校验，`@` 就成了「给任意人推送任意工单摘要」的越权通告渠道。
        finance_user 在财务组，对技术支持组的工单不可见。
        """
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert resolve_mentions(ticket, "@finance1 看这个") == []

    def test_unknown_username_is_ignored(self, tech_group, tech_mailbox):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert resolve_mentions(ticket, "@nobody 在吗") == []

    def test_display_name_is_not_matched(self, tech_group, tech_user, tech_mailbox):
        """只匹配用户名，不匹配显示名 —— 避免重名歧义（契约 §4.4）。"""
        tech_user.first_name = "张"
        tech_user.last_name = "三"
        tech_user.save(update_fields=["first_name", "last_name"])
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert resolve_mentions(ticket, "@张三 在吗") == []

    def test_no_mention_returns_empty(self, tech_group, tech_mailbox):
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert resolve_mentions(ticket, "无提及正文") == []

    def test_superadmin_mention_on_foreign_ticket_is_allowed(
        self, tech_group, superadmin, tech_mailbox
    ):
        """超管本来就能看见全部工单，所以 @ 他应当生效（可见性校验不是"同组"校验）。"""
        ticket = make_ticket(mailbox=tech_mailbox, group=tech_group)
        assert [u.pk for u in resolve_mentions(ticket, "@superadmin")] == [superadmin.pk]
