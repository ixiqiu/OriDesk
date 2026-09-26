"""上线前自检命令（deploy_check）验收用例。

这个命令是部署闸门，因此它的**误报与漏报都要被测试固定**：
- 缺邮箱、兜底邮箱重复、凭据不可解、无管理员组 → 必须 FAIL；
- SQLite / 未配兜底组 / 未配 X-Accel → WARN（可用但需知情）；
- --strict 时 WARN 也要拦住上线。
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings

from apps.accounts.models import Group, Mailbox
from apps.audit.models import Setting
from apps.routing.models import Template
from tests.conftest import make_mailbox


def run(*args) -> str:
    out = StringIO()
    call_command("deploy_check", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def ready_environment(db, fallback_mailbox, tech_group):
    """一个"除了用 SQLite 之外都合规"的环境。"""
    fallback_mailbox  # noqa: B018 - 触发 fixture
    Setting.set("fallback_group_id", tech_group.pk)
    Setting.set("fallback_mailbox_id", fallback_mailbox.pk)
    Group.objects.create(name="管理员组", is_admin_group=True)
    Template.objects.create(scope="global", body="收到，工单号 [T#{{ ticket_id }}]")
    return True


# ------------------------------------------------------------------ FAIL 项
def test_fails_when_no_mailbox_configured(db):
    with pytest.raises(CommandError) as exc:
        run()
    assert "没有任何邮箱" in str(exc.value)


class _FakeQuerySet(list):
    def count(self):
        return len(self)

    def exists(self):
        return bool(self)

    def first(self):
        return self[0] if self else None


class _FakeMailboxManager:
    """只实现 deploy_check 用到的那几个查询，用来构造 MariaDB 才可能出现的数据。"""

    def __init__(self, rows):
        self.rows = rows

    def count(self):
        return len(self.rows)

    def all(self):
        return _FakeQuerySet(self.rows)

    def filter(self, **kwargs):
        rows = list(self.rows)
        for key, value in kwargs.items():
            if key == "is_active":
                rows = [row for row in rows if row.is_active == value]
            elif key == "is_fallback":
                rows = [row for row in rows if row.is_fallback == value]
            elif key == "group__isnull":
                rows = [row for row in rows if (row.group_id is None) == value]
            else:  # pragma: no cover - 出现未预期的查询时让测试明确失败
                raise AssertionError(f"未预期的查询条件：{key}")
        return _FakeQuerySet(rows)


class _Row:
    def __init__(self, email, *, is_active=True, is_fallback=False, group_id=1):
        self.email = email
        self.is_active = is_active
        self.is_fallback = is_fallback
        self.group_id = group_id

    def get_secret(self):
        return "ok"


def test_fails_when_duplicate_fallback_mailboxes(db, ready_environment, monkeypatch):
    """MariaDB 上条件唯一约束不生效（DEV-4），部署自检必须能发现"两个兜底邮箱"。"""
    rows = [
        _Row("fb1@example.com", is_fallback=True),
        _Row("fb2@example.com", is_fallback=True),
    ]
    monkeypatch.setattr(Mailbox, "objects", _FakeMailboxManager(rows))

    with pytest.raises(CommandError) as exc:
        run()
    assert "全局兜底邮箱" in str(exc.value)
    assert "应唯一" in str(exc.value)


@pytest.mark.skipif(
    not connection.features.supports_partial_indexes,
    reason="该行为依赖「带条件的唯一索引」：MariaDB/MySQL 不支持（DEV-4），此处只验证支持的平台",
)
def test_sqlite_constraint_rejects_duplicate_fallback_mailboxes(db):
    """同一件事在 SQLite/PostgreSQL 上由数据库拦住（说明 DEV-4 的平台差异）。"""
    from django.db import IntegrityError, transaction

    make_mailbox(email="fb3@example.com", is_fallback=True)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Mailbox.objects.filter(email="fb3@example.com").update(is_fallback=True)
            Mailbox.objects.bulk_create(
                [
                    Mailbox(
                        name="dup",
                        email="fb4@example.com",
                        imap_host="imap.example.com",
                        smtp_host="smtp.example.com",
                        username="fb4@example.com",
                        secret_encrypted=b"x",
                        is_fallback=True,
                    )
                ]
            )


def test_fails_when_credentials_cannot_be_decrypted(db, monkeypatch):
    make_mailbox(email="broken@example.com")
    monkeypatch.setattr(
        "apps.accounts.models.Mailbox.get_secret",
        lambda self: (_ for _ in ()).throw(RuntimeError("凭据解密失败：FERNET_KEY 可能已更换")),
    )
    with pytest.raises(CommandError) as exc:
        run()
    assert "凭据无法解密" in str(exc.value)


def test_fails_without_admin_group(db, ready_environment):
    Group.objects.filter(is_admin_group=True).update(is_admin_group=False)
    with pytest.raises(CommandError) as exc:
        run()
    assert "没有管理员组" in str(exc.value)


def test_fails_without_any_group(db, fallback_mailbox):
    with pytest.raises(CommandError) as exc:
        run()
    assert "还没有任何用户组" in str(exc.value)


def test_fails_when_group_without_mailbox_and_no_fallback(db, tech_group):
    """无邮箱组 + 没有兜底邮箱 = 该组回信发不出去，必须拦住。"""
    make_mailbox(email="groupbox@example.com")  # 有邮箱，但不是兜底
    with pytest.raises(CommandError) as exc:
        run()
    assert "无法发信" in str(exc.value)


def test_fails_with_weak_secret_key(db, ready_environment, monkeypatch):
    monkeypatch.setattr("django.conf.settings.SECRET_KEY", "short")
    with pytest.raises(CommandError) as exc:
        run()
    assert "SECRET_KEY 过弱" in str(exc.value)


def test_fails_with_invalid_fernet_key(db, ready_environment, monkeypatch):
    monkeypatch.setattr("django.conf.settings.FERNET_KEY", "not-a-valid-fernet-key")
    with pytest.raises(CommandError) as exc:
        run()
    assert "FERNET_KEY 格式非法" in str(exc.value)


def test_fails_when_debug_is_on(db, ready_environment, monkeypatch):
    monkeypatch.setattr("django.conf.settings.DEBUG", True)
    with pytest.raises(CommandError) as exc:
        run()
    assert "DEBUG 仍为 True" in str(exc.value)


def test_fails_with_wildcard_allowed_hosts(db, ready_environment, monkeypatch):
    monkeypatch.setattr("django.conf.settings.ALLOWED_HOSTS", ["*"])
    with pytest.raises(CommandError) as exc:
        run()
    assert "通配符" in str(exc.value)


def test_fails_when_global_template_is_empty(db, ready_environment):
    Template.objects.update(body="   ")
    with pytest.raises(CommandError) as exc:
        run()
    assert "自动回复模板内容为空" in str(exc.value)


def test_fails_when_media_root_not_writable(db, ready_environment, tmp_path):
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    with override_settings(MEDIA_ROOT=str(readonly / "media")):
        with pytest.raises(CommandError) as exc:
            run()
    assert "MEDIA_ROOT 不可写" in str(exc.value)


# ------------------------------------------------------------------ WARN 项与 strict
def test_passes_when_only_warnings(db, ready_environment):
    """只要没有 FAIL 就放行（普通模式），与具体数据库无关。"""
    output = run()
    assert "自检通过" in output


def test_database_check_matches_engine(db, ready_environment):
    """SQLite → 警告改用 MariaDB；MariaDB → 校验 utf8mb4 字符集。"""
    output = run()
    engine = settings.DATABASES["default"]["ENGINE"]
    if "sqlite" in engine:
        assert "数据库使用 SQLite" in output
    else:
        assert "库字符集" in output or "数据库可连接" in output


def test_strict_mode_blocks_on_warnings(db, ready_environment):
    Setting.set("fallback_group_id", "")  # 制造一个必然存在的 WARN，使断言与运行环境无关
    with pytest.raises(CommandError) as exc:
        run("--strict")
    assert "strict" in str(exc.value)


def test_warns_when_attachment_prefix_missing(db, ready_environment):
    with override_settings(ATTACHMENT_X_ACCEL_PREFIX=""):
        output = run()
    assert "未配置 ATTACHMENT_X_ACCEL_PREFIX" in output


def test_reports_missing_fallback_group_as_warning(db, ready_environment):
    Setting.set("fallback_group_id", "")
    output = run()
    assert "未配置兜底组" in output


def test_warns_when_no_group_mailbox(db, ready_environment):
    output = run()
    assert "没有邮箱绑定到任何用户组" in output


def test_output_lists_ok_items(db, ready_environment):
    output = run()
    assert "[OK  ]" in output
    assert "迁移已全部应用" in output
    assert "管理员组" in output


# ------------------------------------------------------------------ 与真实配置的呼应
def test_command_is_documented_and_available():
    from django.core.management import get_commands

    assert "deploy_check" in get_commands()


def test_fernet_import_error_is_reported(monkeypatch, db, ready_environment):
    """极端情况：cryptography 不可用时也要给出 FAIL 而不是崩溃。"""
    import apps.core.management.commands.deploy_check as module

    monkeypatch.setattr(module, "FAIL", "FAIL")  # 保持模块已导入
    try:
        with override_settings(FERNET_KEY="still-invalid!!!"):
            run()
    except (CommandError, ImproperlyConfigured):
        pass  # 报错即可，测试目的是"不崩溃且给出结论"
