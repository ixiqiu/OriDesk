"""审计日志 CSV 导出的安全用例（CSV 注入防护）。"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog


@pytest.fixture
def admin_member(db, admin_group):
    from apps.accounts.models import User, UserGroup

    user = User.objects.create_user(username="auditor", password="DemoPass!2345")
    UserGroup.objects.create(user=user, group=admin_group, is_admin=True)
    return user


def test_csv_export_escapes_formula_injection(client, admin_member):
    """以 = + - @ 开头的单元格会被 Excel 当公式执行，导出时必须加前缀中和。"""
    AuditLog.objects.create(
        action="config_change",
        user=admin_member,
        identity_email="=HYPERLINK(\"http://evil.example\",\"click\")",
        detail={"subject": "=cmd|'/C calc'!A0"},
    )
    client.force_login(admin_member)

    response = client.get(reverse("audit:log_list"), {"export": "csv"})
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")

    body = response.content.decode("utf-8-sig")
    assert "'=HYPERLINK" in body  # 已中和
    # 不存在任何以 = 开头的字段值（表头与行首字段除外：行首是时间戳）
    for line in body.splitlines()[1:]:
        for cell in line.split(","):
            assert not cell.startswith("=HYPERLINK")
            assert not cell.startswith("=cmd")


def test_csv_export_has_header_and_rows(client, admin_member):
    AuditLog.objects.create(action="login", user=admin_member, detail={"username": "auditor"})
    client.force_login(admin_member)

    response = client.get(reverse("audit:log_list"), {"export": "csv"})
    body = response.content.decode("utf-8-sig")
    lines = [line for line in body.splitlines() if line.strip()]
    assert len(lines) >= 2
    assert "时间" in lines[0]
