"""已关闭工单的语义：不再算「待回复」，也不再提示「未认领」。

用户反馈：关掉的工单仍带着「待回复」徽标，还继续出现在待回复队列与角标计数里，
列表的归属列还写着「未认领」—— 看起来像"关了还在催"。

根因是两层：
  1) `set_status()` 关闭工单时没有清 `is_awaiting_reply`，标记残留；
  2) 待回复队列 / 计数 / 徽标都只看这个标记，不看工单状态。

「待回复」的语义是"等客户回信"（§5.5），工单关闭后不成立，因此三处一起收紧：
关闭即清标记、查询排除已关闭、展示对已关闭隐藏。
"""

from __future__ import annotations

import pytest
from django.test import Client

from apps.core.templatetags.ui import awaiting_badge
from apps.tickets.selectors import pending_for_user
from apps.tickets.services import set_status
from tests.conftest import make_ticket


@pytest.fixture
def awaiting_ticket(unified_mailbox, tech_group):
    """一封刚进线、待回复、未认领的工单。"""
    return make_ticket(
        mailbox=unified_mailbox,
        group=tech_group,
        subject="打印机坏了",
        is_awaiting_reply=True,
    )


# ------------------------------------------------------------- 关闭即清标记
def test_closing_ticket_clears_awaiting_flag(awaiting_ticket):
    assert awaiting_ticket.is_awaiting_reply is True

    set_status(awaiting_ticket, "closed")
    awaiting_ticket.refresh_from_db()

    assert awaiting_ticket.status == "closed"
    assert awaiting_ticket.is_awaiting_reply is False, "关闭工单后不应残留待回复标记"


def test_closing_already_clean_ticket_is_noop(awaiting_ticket):
    """本来就没有标记时，关闭不报错、也不写多余的字段。"""
    awaiting_ticket.is_awaiting_reply = False
    awaiting_ticket.save(update_fields=["is_awaiting_reply"])

    set_status(awaiting_ticket, "closed")
    awaiting_ticket.refresh_from_db()
    assert awaiting_ticket.status == "closed"
    assert awaiting_ticket.is_awaiting_reply is False


# ------------------------------------------------------------- 徽标展示
def test_awaiting_badge_hidden_for_closed_ticket(awaiting_ticket, superadmin):
    assert "待回复" in str(awaiting_badge(awaiting_ticket))

    set_status(awaiting_ticket, "closed")
    awaiting_ticket.refresh_from_db()

    assert str(awaiting_badge(awaiting_ticket)) == ""


def test_awaiting_badge_hidden_even_if_flag_left_behind(awaiting_ticket):
    """防御历史脏数据：即使标记还在，已关闭工单也不显示徽标。"""
    set_status(awaiting_ticket, "closed")
    awaiting_ticket.refresh_from_db()
    awaiting_ticket.is_awaiting_reply = True
    awaiting_ticket.save(update_fields=["is_awaiting_reply"])

    assert str(awaiting_badge(awaiting_ticket)) == ""


# ------------------------------------------------------------- 队列与计数
def test_pending_queue_excludes_closed_ticket(awaiting_ticket, superadmin):
    assert pending_for_user(superadmin).filter(pk=awaiting_ticket.pk).exists()

    set_status(awaiting_ticket, "closed")

    assert not pending_for_user(superadmin).filter(pk=awaiting_ticket.pk).exists()


def test_pending_queue_excludes_closed_even_with_stale_flag(awaiting_ticket, superadmin):
    set_status(awaiting_ticket, "closed")
    awaiting_ticket.refresh_from_db()
    awaiting_ticket.is_awaiting_reply = True  # 模拟历史脏数据
    awaiting_ticket.save(update_fields=["is_awaiting_reply"])

    assert not pending_for_user(superadmin).filter(pk=awaiting_ticket.pk).exists()


# ------------------------------------------------------------- 页面表现
@pytest.fixture
def admin_client(superadmin):
    client = Client()
    client.force_login(superadmin)
    return client


def test_awaiting_scope_and_counts_skip_closed(admin_client, awaiting_ticket):
    response = admin_client.get("/awaiting/")
    assert response.status_code == 200
    assert awaiting_ticket.pk in [t.pk for t in response.context["tickets"]]
    assert response.context["scope_counts"]["awaiting"] == 1

    set_status(awaiting_ticket, "closed")

    response = admin_client.get("/awaiting/")
    assert awaiting_ticket.pk not in [t.pk for t in response.context["tickets"]]
    assert response.context["scope_counts"]["awaiting"] == 0, "chips 计数不应再把已关闭工单算进来"


def test_awaiting_filter_option_skips_closed(admin_client, awaiting_ticket):
    response = admin_client.get("/?awaiting=1")
    assert awaiting_ticket.pk in [t.pk for t in response.context["tickets"]]

    set_status(awaiting_ticket, "closed")

    response = admin_client.get("/?awaiting=1")
    assert awaiting_ticket.pk not in [t.pk for t in response.context["tickets"]]


def test_inbox_hides_unassigned_hint_for_closed(admin_client, awaiting_ticket):
    """已关闭且无人认领的工单，列表行里不再提示「 · 未认领」。

    注意「未认领」这三个字本身还出现在筛选 chips 与筛选弹层的选项里（那是入口，不是状态），
    所以断言只针对行内文案的「 · 未认领」。
    """
    body = admin_client.get("/").content.decode()
    assert " · 未认领" in body

    set_status(awaiting_ticket, "closed")

    body = admin_client.get("/").content.decode()
    assert " · 未认领" not in body, "已关闭工单不应再提示未认领"


def test_detail_shows_dash_for_closed(admin_client, awaiting_ticket):
    """详情页「待回复」字段：已关闭显示「—」，不再回答 是/否。"""
    open_body = admin_client.get(f"/tickets/{awaiting_ticket.pk}/").content.decode()
    assert "待回复</span><span>是</span>" in open_body.replace("\n", "").replace(" ", "")

    set_status(awaiting_ticket, "closed")
    closed_body = admin_client.get(f"/tickets/{awaiting_ticket.pk}/").content.decode()
    compact = closed_body.replace("\n", "").replace(" ", "")
    assert "待回复</span><span>—</span>" in compact
    assert "待回复</span><span>是</span>" not in compact
