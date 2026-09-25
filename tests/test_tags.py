"""标签模型与服务验收用例（Tag / TicketTag / add_tag 规则动作）。

背景：开发文档 §4.4 定义了 add_tag 动作，但 §4.3 冻结模型没有标签载体。
经人工确认（§10.4）新增 Tag / TicketTag 后，本文件固定其不变量与行为。
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.audit.models import AuditLog
from apps.routing.models import Rule
from apps.routing.services import apply_rule_actions
from apps.tickets.models import Tag, TicketTag
from apps.tickets.services import (
    add_tag,
    remove_tag,
    resolve_tag,
    tags_available_for,
    ticket_tag_names,
)
from tests.conftest import make_ticket
from tests.helpers import build_email

CUSTOMER = "customer@customer-domain.com"


@pytest.fixture
def ticket(unified_mailbox, tech_group):
    return make_ticket(mailbox=unified_mailbox, group=tech_group, customer_email=CUSTOMER)


# ------------------------------------------------------------------ 模型不变量
def test_tag_name_is_normalized(db):
    tag = Tag.objects.create(name="  合同   审核  ")
    assert tag.name == "合同 审核"


def test_tag_name_cannot_be_blank(db):
    with pytest.raises(ValidationError):
        Tag.objects.create(name="   ")


def test_global_tag_name_must_be_unique(db):
    """group 为 NULL 时 SQL 唯一约束不生效，必须由模型层兜底。"""
    Tag.objects.create(name="紧急")
    with pytest.raises(ValidationError):
        Tag.objects.create(name="紧急")
    assert Tag.objects.filter(name="紧急").count() == 1


def test_same_name_allowed_in_different_groups(db, tech_group, finance_group):
    Tag.objects.create(name="紧急", group=tech_group)
    Tag.objects.create(name="紧急", group=finance_group)
    assert Tag.objects.filter(name="紧急").count() == 2


def test_group_scoped_name_unique_enforced_by_db_too(db, tech_group):
    Tag.objects.create(name="发票", group=tech_group)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            # 绕过 save() 的应用层校验，直接走数据库约束
            Tag.objects.bulk_create([Tag(name="发票", group=tech_group)])


def test_updating_name_to_existing_is_rejected(db, tech_group):
    first = Tag.objects.create(name="甲", group=tech_group)
    second = Tag.objects.create(name="乙", group=tech_group)
    second.name = "甲"
    with pytest.raises(ValidationError):
        second.save()
    first.refresh_from_db()
    assert first.name == "甲"


# ------------------------------------------------------------------ 解析与可用范围
def test_resolve_tag_prefers_group_scope_then_global(db, tech_group, finance_group):
    global_tag = Tag.objects.create(name="紧急")
    group_tag = Tag.objects.create(name="紧急", group=tech_group)

    assert resolve_tag("紧急", group=tech_group) == group_tag
    assert resolve_tag("紧急", group=finance_group) == global_tag


def test_resolve_tag_creates_in_group_scope(db, tech_group):
    tag = resolve_tag("合同", group=tech_group)
    assert tag.group_id == tech_group.pk


def test_resolve_tag_without_create_returns_none(db):
    assert resolve_tag("不存在", create=False) is None


def test_tags_available_for_includes_global_and_own_group_only(
    db, tech_group, finance_group
):
    Tag.objects.create(name="全局标签")
    Tag.objects.create(name="技术标签", group=tech_group)
    Tag.objects.create(name="财务标签", group=finance_group)
    Tag.objects.create(name="已停用", group=tech_group, is_active=False)

    names = set(tags_available_for(tech_group).values_list("name", flat=True))
    assert names == {"全局标签", "技术标签"}


# ------------------------------------------------------------------ 打标签 / 取消
def test_add_tag_is_idempotent(ticket, tech_user):
    link, created = add_tag(ticket, "合同", user=tech_user)
    assert created is True
    again, created_again = add_tag(ticket, "合同", user=tech_user)
    assert created_again is False
    assert again.pk == link.pk
    assert TicketTag.objects.filter(ticket=ticket).count() == 1
    assert ticket_tag_names(ticket) == ["合同"]


def test_add_tag_records_actor_and_source(ticket, tech_user):
    link, _ = add_tag(ticket, "合同", user=tech_user, source="manual")
    assert link.added_by == tech_user
    assert link.source == "manual"

    rule_tag = resolve_tag("规则标签", group=ticket.group)
    link2, _ = add_tag(ticket, rule_tag, user=tech_user, source="rule")
    assert link2.added_by is None
    assert link2.source == "rule"


def test_add_tag_writes_audit(ticket, tech_user):
    add_tag(ticket, "合同", user=tech_user)
    log = AuditLog.objects.filter(ticket=ticket, detail__event="tag_added").first()
    assert log is not None
    assert log.detail["tag"] == "合同"
    assert log.user == tech_user


def test_add_tag_can_skip_audit_for_rule_source(ticket, tech_user):
    add_tag(ticket, "合同", user=tech_user, source="rule", record_audit=False)
    assert AuditLog.objects.filter(ticket=ticket, detail__event="tag_added").exists() is False


def test_add_tag_rejects_blank_name(ticket, tech_user):
    with pytest.raises(ValueError):
        add_tag(ticket, "   ", user=tech_user)


def test_remove_tag(ticket, tech_user):
    add_tag(ticket, "合同", user=tech_user)
    tag = Tag.objects.get(name="合同")
    assert remove_tag(ticket, tag, user=tech_user) is True
    assert TicketTag.objects.filter(ticket=ticket).count() == 0
    # 再次移除返回 False，不报错
    assert remove_tag(ticket, tag, user=tech_user) is False
    assert AuditLog.objects.filter(ticket=ticket, detail__event="tag_removed").exists()


def test_remove_tag_by_name(ticket, tech_user):
    add_tag(ticket, "合同", user=tech_user)
    assert remove_tag(ticket, "合同", user=tech_user) is True
    assert remove_tag(ticket, "不存在", user=tech_user) is False


def test_deleting_tag_cascades_ticket_links(ticket, tech_user):
    add_tag(ticket, "合同", user=tech_user)
    Tag.objects.get(name="合同").delete()
    assert TicketTag.objects.filter(ticket=ticket).count() == 0


# ------------------------------------------------------------------ 规则动作落库
def test_rule_action_add_tag_persists(ticket, unified_mailbox, tech_group, tech_user):
    Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="登录",
        action_type="add_tag",
        action_value="登录问题",
    )
    msg = build_email(sender=CUSTOMER, subject="无法登录后台")

    applied = apply_rule_actions(ticket, msg, unified_mailbox, user=tech_user)

    assert applied == ["tag=登录问题"]
    tag = Tag.objects.get(name="登录问题")
    # 落在工单所属组的作用域
    assert tag.group_id == tech_group.pk
    link = TicketTag.objects.get(ticket=ticket, tag=tag)
    assert link.source == "rule"
    assert link.added_by is None
    # 规则动作只写一条汇总审计
    log = AuditLog.objects.filter(ticket=ticket, detail__event="rule_action").first()
    assert log is not None
    assert log.detail["applied"] == ["tag=登录问题"]
    assert AuditLog.objects.filter(ticket=ticket, detail__event="tag_added").exists() is False


def test_rule_action_add_tag_twice_keeps_single_link(
    ticket, unified_mailbox, tech_user, outbox
):
    Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="登录",
        action_type="add_tag",
        action_value="登录问题",
    )
    msg = build_email(sender=CUSTOMER, subject="无法登录后台")
    apply_rule_actions(ticket, msg, unified_mailbox, user=tech_user)
    applied = apply_rule_actions(ticket, msg, unified_mailbox, user=tech_user)

    assert applied == ["tag=登录问题(已存在)"]
    assert TicketTag.objects.filter(ticket=ticket).count() == 1


def test_pipeline_applies_tag_rule_on_new_ticket(
    unified_mailbox, tech_group, outbox, settings, tmp_path
):
    """端到端：入站邮件命中 add_tag 规则后，工单上真的带上了标签。"""
    from apps.audit.models import Setting
    from apps.mailboxes.pipeline import process_inbound
    from tests.helpers import build_raw

    settings.MEDIA_ROOT = tmp_path
    # add_tag 规则不参与"归属组"决策（只打标签），因此需要兜底组保证路由可完成
    Setting.set("fallback_group_id", tech_group.pk)
    Rule.objects.create(
        mailbox=unified_mailbox,
        priority=10,
        match_field="subject",
        match_op="contains",
        match_value="合同",
        action_type="add_tag",
        action_value="合同",
    )
    result = process_inbound(unified_mailbox, 1, build_raw(sender=CUSTOMER, subject="合同盖章流程"))

    assert result.ticket is not None
    assert ticket_tag_names(result.ticket) == ["合同"]
    assert result.rule_actions == ["tag=合同"]
    # 路由仍然正常完成（落入兜底组），说明"只打标签"的规则不会阻断路由
    assert result.ticket.group_id == tech_group.pk


# ------------------------------------------------------------------ 作用域 / 大小写 / 上限 / 历史标签
def test_tag_name_dedup_is_case_insensitive(db):
    """与 MariaDB utf8mb4_unicode_ci 的归并语义一致，避免生产库抛 IntegrityError。"""
    Tag.objects.create(name="Urgent")
    with pytest.raises(ValidationError):
        Tag.objects.create(name="urgent")
    assert Tag.objects.filter(name__iexact="urgent").count() == 1
    # resolve_tag 也要复用既有标签而不是新建
    assert resolve_tag("URGENT").name == "Urgent"


def test_cannot_tag_with_other_groups_tag(ticket, tech_group, finance_group, tech_user):
    """服务层必须拦下跨组专属标签（界面下拉只是体验）。"""
    finance_tag = Tag.objects.create(name="发票", group=finance_group)
    with pytest.raises(ValueError) as exc:
        add_tag(ticket, finance_tag, user=tech_user)
    assert "财务组" in str(exc.value)
    assert TicketTag.objects.filter(ticket=ticket).count() == 0


def test_global_tag_can_be_used_by_any_group(ticket, tech_user):
    global_tag = Tag.objects.create(name="紧急")
    add_tag(ticket, global_tag, user=tech_user)
    assert ticket_tag_names(ticket) == ["紧急"]


def test_disabled_tag_is_rejected(ticket, tech_user):
    tag = Tag.objects.create(name="已停用标签", group=ticket.group, is_active=False)
    with pytest.raises(ValueError) as exc:
        add_tag(ticket, tag, user=tech_user)
    assert "已停用" in str(exc.value)
    # 停用后同名标签无法通过名称再次打上（会被判定为不可用）
    with pytest.raises(ValueError) as exc2:
        add_tag(ticket, "已停用标签", user=tech_user)
    assert "已停用" in str(exc2.value)


def test_disabled_tag_history_is_preserved(ticket, tech_user):
    add_tag(ticket, "历史标签", user=tech_user)
    tag = Tag.objects.get(name="历史标签")
    tag.is_active = False
    tag.save(update_fields=["is_active"])
    # 已打的标签仍然可读（历史保留），只是不能再新打
    assert ticket_tag_names(ticket) == ["历史标签"]


def test_tag_count_limit_enforced(ticket, tech_user):
    from apps.tickets.models import MAX_TAGS_PER_TICKET

    for index in range(MAX_TAGS_PER_TICKET):
        add_tag(ticket, f"标签{index:02d}", user=tech_user)
    assert TicketTag.objects.filter(ticket=ticket).count() == MAX_TAGS_PER_TICKET

    # 已达上限：新增被拒
    with pytest.raises(ValueError) as exc:
        add_tag(ticket, "再来一个", user=tech_user)
    assert str(MAX_TAGS_PER_TICKET) in str(exc.value)

    # 但重复添加已存在的标签仍然幂等通过（不占新额度）
    link, created = add_tag(ticket, "标签00", user=tech_user)
    assert created is False
    assert TicketTag.objects.filter(ticket=ticket).count() == MAX_TAGS_PER_TICKET


def test_foreign_tag_after_reassign(ticket, tech_group, finance_group, tech_user):
    """改派后原组专属标签保留为历史标签，全局标签不受影响。"""
    from apps.routing.services import reassign_ticket

    add_tag(ticket, "技术专属", user=tech_user)
    add_tag(ticket, Tag.objects.create(name="紧急"), user=tech_user)
    reassign_ticket(ticket, finance_group, tech_user)

    ticket.refresh_from_db()
    assert ticket.group == finance_group
    # 标签没有被删除（保留历史事实）
    assert set(ticket_tag_names(ticket)) == {"技术专属", "紧急"}

    flags = {link.tag.name: link.is_foreign for link in ticket.ticket_tags.select_related("tag")}
    assert flags["技术专属"] is True
    assert flags["紧急"] is False


def test_rejected_tag_does_not_leave_orphan_tag_row(ticket, tech_user):
    """被"超过上限"拒绝的请求不得在标签字典里留下孤立标签（服务层顺序修正）。"""
    from apps.tickets.models import MAX_TAGS_PER_TICKET

    for index in range(MAX_TAGS_PER_TICKET):
        add_tag(ticket, f"容量{index:02d}", user=tech_user)

    before = Tag.objects.count()
    with pytest.raises(ValueError):
        add_tag(ticket, "第二十一个", user=tech_user)
    assert Tag.objects.count() == before
    assert not Tag.objects.filter(name="第二十一个").exists()


def test_find_tag_reports_best_match_for_error_messages(ticket, tech_group, finance_group):
    """find_tag 永不创建，且优先返回本组标签，便于给出准确提示。"""
    from apps.tickets.services import find_tag

    global_tag = Tag.objects.create(name="紧急")
    assert find_tag("紧急", group=tech_group) == global_tag
    assert find_tag("不存在", group=tech_group) is None
    foreign = Tag.objects.create(name="发票", group=finance_group)
    # 同名标签只在别的组存在时，也要能查到它（用于提示"属于用户组 X"）
    assert find_tag("发票", group=tech_group) == foreign
    assert Tag.objects.count() == 2  # 没有任何隐式创建
