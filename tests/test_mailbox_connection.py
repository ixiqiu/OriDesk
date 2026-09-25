"""邮箱接入验收用例：服务商预设、连接工厂（SSL / STARTTLS）、图形化配置流程。

对应问题："怎么连我的邮箱？不是飞书能不能连？"
结论用测试固定下来：接入层只依赖标准 IMAP/SMTP，任何服务商都可配置，飞书只是一个预设。
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import Mailbox, User
from apps.accounts.provider_presets import (
    MAILBOX_PROVIDER_PRESETS,
    PRESETS_BY_KEY,
    PROVIDER_CHOICES,
)
from apps.mailboxes.imap_client import ImapConnectionError, connect_imap
from tests.conftest import make_mailbox


# --------------------------------------------------------------- 服务商预设数据
def test_presets_are_well_formed():
    keys = [item["key"] for item in MAILBOX_PROVIDER_PRESETS]
    assert len(keys) == len(set(keys)), "预设 key 不能重复"
    for item in MAILBOX_PROVIDER_PRESETS:
        assert item["label"]
        assert isinstance(item["imap_port"], int) and 1 <= item["imap_port"] <= 65535
        assert isinstance(item["smtp_port"], int) and 1 <= item["smtp_port"] <= 65535
        assert isinstance(item["imap_ssl"], bool)
        assert isinstance(item["smtp_ssl"], bool)
        assert item["auth_hint"], f"{item['key']} 缺少填写提示"
        if item["key"] != "custom":
            assert item["imap_host"] and item["smtp_host"], f"{item['key']} 缺少主机"


def test_provider_choices_cover_all_presets():
    keys = {key for key, _label in PROVIDER_CHOICES}
    assert "" in keys  # 允许"不套用预设"
    for item in MAILBOX_PROVIDER_PRESETS:
        assert item["key"] in keys


def test_presets_include_non_feishu_providers():
    """明确断言：飞书只是其中之一，接入不绑定飞书。"""
    keys = set(PRESETS_BY_KEY)
    assert "feishu" in keys
    for expected in ("tencent_exmail", "aliyun", "netease_163", "gmail", "outlook365", "self_hosted", "custom"):
        assert expected in keys


def test_outlook_uses_starttls_on_587():
    """Microsoft 365 的 587 走 STARTTLS（smtp_ssl=False），不是隐式 TLS。"""
    preset = PRESETS_BY_KEY["outlook365"]
    assert preset["smtp_port"] == 587
    assert preset["smtp_ssl"] is False


# --------------------------------------------------------------- IMAP 连接工厂
class FakeImapClient:
    """imapclient.IMAPClient 的替身，用于验证加密策略而非真实网络。"""

    def __init__(self, host, port=None, ssl=True, timeout=None):
        self.host = host
        self.port = port
        self.ssl = ssl
        self.capabilities_tuple = (b"IMAP4REV1", b"STARTTLS", b"AUTH=PLAIN")
        self.starttls_called = False
        self.login_called_after_starttls: bool | None = None
        self.credentials = None
        self.closed = False
        self.login_error: Exception | None = None

    # 能力查询
    def has_capability(self, name):
        return name.upper().encode() in self.capabilities_tuple

    def capabilities(self):
        return self.capabilities_tuple

    # 协议动作
    def starttls(self, ssl_context=None):
        if self.ssl:
            raise AssertionError("隐式 TLS 连接不应再执行 STARTTLS")
        self.starttls_called = True

    def login(self, username, secret):
        self.login_called_after_starttls = self.starttls_called
        if self.login_error:
            raise self.login_error
        self.credentials = (username, secret)

    def logout(self):
        self.closed = True

    def select_folder(self, folder):
        return {b"EXISTS": 2}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


def install_fake(monkeypatch, **overrides):
    """替换 imapclient.IMAPClient：返回工厂创建的 client（ssl 等参数与调用方一致）。"""
    created = []

    def factory(host, port=None, ssl=True, timeout=None, **kwargs):
        instance = FakeImapClient(host, port=port, ssl=ssl, timeout=timeout)
        for key, value in overrides.items():
            setattr(instance, key, value)
        created.append(instance)
        return instance

    monkeypatch.setattr("imapclient.IMAPClient", factory)
    return created


def test_connect_imap_with_ssl_uses_implicit_tls(db, monkeypatch):
    mailbox = make_mailbox(email="ssl@example.com")
    assert mailbox.imap_ssl is True
    created = install_fake(monkeypatch)

    returned = connect_imap(mailbox, "auth-code")
    client = created[0]

    assert returned is client
    assert client.starttls_called is False  # 已经是加密连接
    assert client.credentials == (mailbox.username, "auth-code")


def test_connect_imap_without_ssl_upgrades_via_starttls(db, monkeypatch):
    mailbox = make_mailbox(email="plain@example.com")
    mailbox.imap_ssl = False
    mailbox.imap_port = 143
    mailbox.save(update_fields=["imap_ssl", "imap_port"])
    created = install_fake(monkeypatch)

    connect_imap(mailbox, "auth-code")
    client = created[0]

    assert client.ssl is False
    assert client.starttls_called is True
    assert client.login_called_after_starttls is True  # 先升级加密、再提交凭据


def test_connect_imap_refuses_plaintext_when_starttls_unavailable(db, monkeypatch):
    """服务器不支持 STARTTLS 时必须报错，绝不退回明文登录。"""
    mailbox = make_mailbox(email="noplain@example.com")
    mailbox.imap_ssl = False
    mailbox.save(update_fields=["imap_ssl"])
    created = install_fake(monkeypatch, capabilities_tuple=(b"IMAP4REV1",))

    with pytest.raises(ImapConnectionError) as exc:
        connect_imap(mailbox, "auth-code")
    client = created[0]

    assert "STARTTLS" in str(exc.value)
    assert client.credentials is None  # 没有提交过凭据
    assert client.closed is True


def test_connect_imap_wraps_login_failure(db, monkeypatch):
    mailbox = make_mailbox(email="badlogin@example.com")
    created = install_fake(monkeypatch, login_error=RuntimeError("LOGIN failed"))

    with pytest.raises(ImapConnectionError) as exc:
        connect_imap(mailbox, "wrong-code")
    client = created[0]

    assert "IMAP 登录失败" in str(exc.value)
    assert client.closed is True


def test_connect_imap_wraps_connection_failure(db, monkeypatch):
    mailbox = make_mailbox(email="down@example.com")

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("imapclient.IMAPClient", boom)

    with pytest.raises(ImapConnectionError) as exc:
        connect_imap(mailbox, "auth-code")

    assert "无法连接 IMAP 服务器" in str(exc.value)


# --------------------------------------------------------------- 图形化配置流程
@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username="root", password="DemoPass!2345", is_superadmin=True, is_staff=True, is_superuser=True
    )


def test_mailbox_form_renders_provider_presets(client, admin_user):
    client.force_login(admin_user)
    response = client.get(reverse("accounts:mailbox_create"))
    body = response.content.decode()

    assert response.status_code == 200
    assert 'id="provider-presets"' in body
    assert "服务商预设" in body
    # 非飞书服务商也出现在下拉里，说明界面不绑定飞书
    for label in ("腾讯企业邮箱", "阿里云企业邮箱", "Google Workspace", "自建邮件服务器"):
        assert label in body


def test_create_mailbox_without_js_by_selecting_preset(client, admin_user):
    """无脚本环境：只选服务商预设、主机留空也能保存出正确的连接参数。"""
    client.force_login(admin_user)
    response = client.post(
        reverse("accounts:mailbox_create"),
        {
            "name": "腾讯企业邮",
            "email": "support@corp-example.com",
            "provider": "tencent_exmail",
            "imap_host": "",
            "smtp_host": "",
            "username": "support@corp-example.com",
            "secret": "auth-code-123",
            "is_active": "on",
        },
        follow=True,
    )

    assert response.status_code == 200
    mailbox = Mailbox.objects.get(email="support@corp-example.com")
    assert mailbox.imap_host == "imap.exmail.qq.com"
    assert mailbox.smtp_host == "smtp.exmail.qq.com"
    assert mailbox.imap_port == 993
    assert mailbox.smtp_port == 465
    assert mailbox.get_secret() == "auth-code-123"
    # provider 只是表单便利字段，不入库、不产生模型字段
    assert not hasattr(mailbox, "provider")


def test_explicit_hosts_are_not_overwritten_by_preset(client, admin_user):
    client.force_login(admin_user)
    client.post(
        reverse("accounts:mailbox_create"),
        {
            "name": "自建",
            "email": "ops@self-hosted-example.com",
            "provider": "gmail",  # 故意选错预设
            "imap_host": "imap.my-company.internal",
            "smtp_host": "smtp.my-company.internal",
            "username": "ops",
            "secret": "pw",
            "is_active": "on",
        },
        follow=True,
    )
    mailbox = Mailbox.objects.get(email="ops@self-hosted-example.com")
    assert mailbox.imap_host == "imap.my-company.internal"
    assert mailbox.smtp_host == "smtp.my-company.internal"


def test_form_requires_host_or_preset(client, admin_user):
    """两个都留空（也没有预设）时必须给出字段错误，而不是保存出空主机。"""
    client.force_login(admin_user)
    response = client.post(
        reverse("accounts:mailbox_create"),
        {
            "name": "空配置",
            "email": "empty@example.com",
            "provider": "",
            "imap_host": "",
            "smtp_host": "",
            "username": "empty",
            "secret": "pw",
            "is_active": "on",
        },
    )
    assert response.status_code == 200
    assert Mailbox.objects.filter(email="empty@example.com").exists() is False
    assert "请填写 IMAP 主机" in response.content.decode()


class FakeSMTP:
    """smtplib.SMTP_SSL / SMTP 的替身：只验证登录，且绝不发送邮件。"""

    sent_messages = 0

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.logged_in = None

    def ehlo(self):
        return (250, b"ok")

    def has_extn(self, name):
        return False

    def starttls(self):
        return (220, b"ready")

    def login(self, username, secret):
        self.logged_in = (username, secret)

    def quit(self):
        return (221, b"bye")

    def send_message(self, msg):  # pragma: no cover - 关闭性断言
        raise AssertionError("连通性检查绝不能发送邮件")


def test_mailbox_verify_reports_starttls_problem_without_500(client, admin_user, monkeypatch):
    """连通性检查遇到"未提供 STARTTLS"只提示错误，不 500，也不发任何邮件。"""
    mailbox = make_mailbox(email="verify@example.com")
    mailbox.imap_ssl = False
    mailbox.save(update_fields=["imap_ssl"])

    client.force_login(admin_user)
    install_fake(monkeypatch, capabilities_tuple=(b"IMAP4REV1",))  # 服务器不支持 STARTTLS
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)

    response = client.post(reverse("accounts:mailbox_verify", args=[mailbox.pk]), follow=True)

    assert response.status_code == 200
    assert "STARTTLS" in response.content.decode()


def test_verify_path_reuses_same_encryption_policy(client, admin_user, monkeypatch):
    """连通性检查与定时同步共用 connect_imap，不会出现"检查通过但同步失败"。"""
    mailbox = make_mailbox(email="same@example.com")
    created = install_fake(monkeypatch)
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)

    client.force_login(admin_user)
    response = client.post(reverse("accounts:mailbox_verify", args=[mailbox.pk]), follow=True)

    body = response.content.decode()
    assert response.status_code == 200
    assert created[0].credentials == (mailbox.username, "demo-auth-code")
    assert created[0].starttls_called is False  # SSL 邮箱不做 STARTTLS
    assert "INBOX 共 2 封" in body
    assert "SMTP 登录成功" in body


# --------------------------------------------------------------- 文档与预设保持同步
def test_guide_documents_every_provider_preset():
    """文档漂移守卫：新增预设却忘了更新《邮箱接入指南》，本用例失败。"""
    from pathlib import Path

    guide = Path(__file__).resolve().parent.parent / "docs" / "邮箱接入指南.md"
    text = guide.read_text(encoding="utf-8")

    for item in MAILBOX_PROVIDER_PRESETS:
        if item["key"] == "custom":
            continue
        assert item["imap_host"] in text, f"{item['key']} 的 IMAP 主机未写进指南"
        assert item["smtp_host"] in text, f"{item['key']} 的 SMTP 主机未写进指南"
        assert item["label"] in text, f"{item['key']} 的名称未写进指南"


def test_guide_states_oauth_limitation():
    """已知限制必须留在文档里，避免被"看起来什么都支持"误导。"""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "docs" / "邮箱接入指南.md").read_text(encoding="utf-8")
    assert "OAuth2" in text
    assert "OAuth2 / XOAUTH2" in text or "XOAUTH2" in text


def test_code_has_no_hardcoded_feishu_dependency():
    """接入层不得出现飞书专属逻辑（只允许作为预设数据/文档措辞出现）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    suspicious = []
    for path in (root / "apps").rglob("*.py"):
        if "provider_presets" in path.name:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "feishu" in line.lower() and "预设" not in line and "preset" not in line.lower():
                suspicious.append(f"{path}:{lineno}: {line.strip()}")
    assert suspicious == [], "发现疑似飞书专属逻辑：" + "; ".join(suspicious)


# --------------------------------------------------------------- 权限：谁可以配置邮箱
def test_group_admin_can_manage_mailboxes(client, db, tech_group):
    """决定（2026-09）：可跨组查看的管理者（组内管理员/管理员组成员）也能配置邮箱。"""
    from apps.accounts.models import UserGroup

    user = User.objects.create_user(username="groupadmin", password="DemoPass!2345")
    UserGroup.objects.create(user=user, group=tech_group, is_admin=True)
    client.force_login(user)

    assert client.get(reverse("accounts:mailbox_list")).status_code == 200
    assert client.get(reverse("accounts:mailbox_create")).status_code == 200


def test_admin_group_member_can_manage_mailboxes(client, db, admin_group):
    from apps.accounts.models import UserGroup

    user = User.objects.create_user(username="admingroupmember", password="DemoPass!2345")
    UserGroup.objects.create(user=user, group=admin_group)
    client.force_login(user)

    assert client.get(reverse("accounts:mailbox_list")).status_code == 200


def test_plain_member_still_forbidden_on_mailboxes(client, db, tech_group):
    """普通组员仍然无权：放开的是管理者，不是所有人。"""
    from apps.accounts.models import UserGroup

    user = User.objects.create_user(username="plainmember", password="DemoPass!2345")
    UserGroup.objects.create(user=user, group=tech_group)  # 非组内管理员
    client.force_login(user)

    assert client.get(reverse("accounts:mailbox_list")).status_code == 403
    assert client.get(reverse("accounts:mailbox_create")).status_code == 403


def test_group_admin_cannot_manage_users_or_groups(client, db, tech_group):
    """邮箱配置放开后，用户/用户组管理仍只属于超级管理员（两者已解耦）。"""
    from apps.accounts.models import UserGroup

    user = User.objects.create_user(username="groupadmin2", password="DemoPass!2345")
    UserGroup.objects.create(user=user, group=tech_group, is_admin=True)
    client.force_login(user)

    assert client.get(reverse("accounts:user_list")).status_code == 403
    assert client.get(reverse("accounts:group_list")).status_code == 403
    # 但仍然可以配置邮箱
    assert client.get(reverse("accounts:mailbox_list")).status_code == 200


def test_mailbox_save_writes_audit_without_credentials(client, admin_user):
    """放开权限后必须有审计：记录改了哪些字段，但绝不记录凭据明文。"""
    from apps.audit.models import AuditLog

    client.force_login(admin_user)
    client.post(
        reverse("accounts:mailbox_create"),
        {
            "name": "审计用邮箱",
            "email": "audit-box@example.com",
            "provider": "gmail",
            "imap_host": "",
            "smtp_host": "",
            "username": "audit-box@example.com",
            "secret": "super-secret-code",
            "is_active": "on",
        },
        follow=True,
    )

    log = AuditLog.objects.filter(detail__event="mailbox_created").first()
    assert log is not None
    assert log.detail["mailbox"] == "audit-box@example.com"
    assert log.detail["credentials_updated"] is True
    assert log.user == admin_user
    assert "super-secret-code" not in str(log.detail)
    assert "super-secret-code" not in (log.detail.get("changed_fields") or [])


def test_mailbox_edit_audit_lists_changed_fields(client, admin_user):
    from apps.audit.models import AuditLog

    mailbox = make_mailbox(email="edit-audit@example.com")
    client.force_login(admin_user)
    client.post(
        reverse("accounts:mailbox_edit", args=[mailbox.pk]),
        {
            "name": "改名后的邮箱",
            "email": mailbox.email,
            "provider": "",
            "imap_host": mailbox.imap_host,
            "imap_port": mailbox.imap_port,
            "imap_ssl": "on",
            "smtp_host": mailbox.smtp_host,
            "smtp_port": mailbox.smtp_port,
            "smtp_ssl": "on",
            "username": mailbox.username,
            "secret": "",
            "is_active": "on",
        },
        follow=True,
    )

    log = AuditLog.objects.filter(detail__event="mailbox_updated").first()
    assert log is not None
    assert "name" in log.detail["changed_fields"]
    assert log.detail["credentials_updated"] is False


def test_nav_shows_mailbox_entry_for_group_admin(client, db, tech_group):
    from apps.accounts.models import UserGroup

    user = User.objects.create_user(username="navadmin", password="DemoPass!2345")
    UserGroup.objects.create(user=user, group=tech_group, is_admin=True)
    client.force_login(user)

    body = client.get(reverse("tickets:inbox")).content.decode()
    assert reverse("accounts:mailbox_list") in body  # 导航里出现"邮箱配置"
    assert reverse("accounts:user_list") not in body  # 但用户管理不出现
