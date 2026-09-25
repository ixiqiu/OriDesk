"""账号管理的集成约定（密码策略、超管权限同步、会话保持）。"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import User

PASSWORD = "DemoPass!2345"


@pytest.fixture
def superadmin(db):
    return User.objects.create_user(
        username="root", password=PASSWORD, is_superadmin=True, is_staff=True, is_superuser=True
    )


def test_create_superadmin_via_ui_grants_admin_access(client, superadmin):
    client.force_login(superadmin)
    response = client.post(
        reverse("accounts:user_create"),
        {
            "username": "newadmin",
            "first_name": "新管理员",
            "last_name": "",
            "email": "newadmin@example.com",
            "is_active": "on",
            "is_superadmin": "on",
            "password": PASSWORD,
        },
        follow=True,
    )
    assert response.status_code == 200
    created = User.objects.get(username="newadmin")
    assert created.is_superadmin is True
    # 应用层超管必须能进 Django Admin（否则导航里的入口是死链）
    assert created.is_staff is True
    assert created.is_superuser is True
    assert created.check_password(PASSWORD)


def test_weak_password_rejected(client, superadmin):
    client.force_login(superadmin)
    response = client.post(
        reverse("accounts:user_create"),
        {
            "username": "weakuser",
            "first_name": "",
            "last_name": "",
            "email": "weak@example.com",
            "is_active": "on",
            "password": "123456",
        },
    )
    assert response.status_code == 200  # 回到表单并显示错误
    assert not User.objects.filter(username="weakuser").exists()
    assert "密码" in response.content.decode()


def test_demoting_superadmin_revokes_admin_access(client, superadmin):
    client.force_login(superadmin)
    other = User.objects.create_user(
        username="second", password=PASSWORD, is_superadmin=True, is_staff=True, is_superuser=True
    )
    response = client.post(
        reverse("accounts:user_edit", args=[other.pk]),
        {
            "username": "second",
            "first_name": "",
            "last_name": "",
            "email": "second@example.com",
            "is_active": "on",
            "password": "",
        },
        follow=True,
    )
    assert response.status_code == 200
    other.refresh_from_db()
    assert other.is_superadmin is False
    assert other.is_staff is False
    assert other.is_superuser is False


def test_editing_own_password_keeps_session(client, superadmin):
    client.force_login(superadmin)
    response = client.post(
        reverse("accounts:user_edit", args=[superadmin.pk]),
        {
            "username": "root",
            "first_name": "",
            "last_name": "",
            "email": "root@example.com",
            "is_active": "on",
            "is_superadmin": "on",
            "password": "NewDemoPass!2345",
        },
        follow=True,
    )
    assert response.status_code == 200
    superadmin.refresh_from_db()
    assert superadmin.check_password("NewDemoPass!2345")
    # 会话未被踢下线
    assert client.get(reverse("accounts:profile")).status_code == 200


def test_non_superadmin_cannot_create_users(client, tech_user):
    client.force_login(tech_user)
    assert client.get(reverse("accounts:user_create")).status_code == 403
    assert client.post(reverse("accounts:user_create"), {"username": "x"}).status_code == 403
