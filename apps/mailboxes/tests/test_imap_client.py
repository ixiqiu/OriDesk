"""``apps.mailboxes.imap_client`` 单元测试：RFC 2971 客户端 ID（`send_client_id`）。

背景：网易个人邮箱（163 / 126 / yeah，Coremail 平台）要求第三方客户端在登录后先发送
``ID`` 命令声明身份，否则后续 ``SELECT`` 会被拒绝并报
``Unsafe Login. Please contact kefu@188.com for help``。

因此这里锁住三条不变量（缺一条都会波及别的服务商）：
1. 服务器**未声明** ``ID`` 能力 → 一条命令都不许发（飞书 / Gmail 等不认该命令）；
2. 服务器声明了 ``ID`` 能力 → 按 RFC 2971 发送 ``name`` / ``version`` / ``vendor``；
3. 能力查询或 ID 发送失败 → 只记日志、绝不抛出（可选命令不得搞挂已建立的会话）。

另有两条集成断言：``connect_imap()`` 必须**在登录成功之后**才发送 ID，且不支持 ID 的
服务商仍能正常拿到连接（回归保护）。
"""

from __future__ import annotations

import logging
import types

import pytest

from apps.mailboxes.imap_client import (
    CLIENT_ID_NAME,
    CLIENT_ID_VENDOR,
    CLIENT_ID_VERSION,
    connect_imap,
    send_client_id,
)


def _mailbox(**overrides):
    """connect_imap / send_client_id 只用这几个属性，无需落库。"""
    base = {
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_ssl": True,
        "username": "user@example.com",
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FakeIMAPClient:
    """最小 IMAP 客户端替身（只实现被调用到的接口）。"""

    def __init__(
        self,
        *,
        supports_id: bool = True,
        capabilities_error=None,
        id_error=None,
        enforce_login_order: bool = False,
    ):
        self._supports_id = supports_id
        self._capabilities_error = capabilities_error
        self._id_error = id_error
        self._enforce_login_order = enforce_login_order
        self.id_calls: list = []
        self.login_calls: list = []
        self._logged_in = False

    def has_capability(self, name):
        if self._capabilities_error is not None:
            raise self._capabilities_error
        return bool(self._supports_id and str(name).upper() == "ID")

    def login(self, username, secret):
        self.login_calls.append((username, secret))
        self._logged_in = True

    def id_(self, parameters=None):
        if self._enforce_login_order:
            # 网易要求 ID 出现在登录之后；顺序错了这里直接失败
            assert self._logged_in, "ID 必须在登录成功之后发送"
        if self._id_error is not None:
            raise self._id_error
        self.id_calls.append(parameters)
        return {"name": "Coremail"}

    def quit(self):  # pragma: no cover - 替身无需真实登出
        pass


# ------------------------------------------------------------- send_client_id
class TestSendClientId:
    """`send_client_id` 的兼容性不变量。"""

    def test_skips_when_server_does_not_advertise_id(self):
        client = _FakeIMAPClient(supports_id=False)

        send_client_id(client, _mailbox())

        assert client.id_calls == []

    def test_sends_name_version_vendor_when_supported(self):
        client = _FakeIMAPClient(supports_id=True)

        send_client_id(client, _mailbox())

        assert client.id_calls == [
            {
                "name": CLIENT_ID_NAME,
                "version": CLIENT_ID_VERSION,
                "vendor": CLIENT_ID_VENDOR,
            }
        ]

    def test_capability_lookup_failure_is_silent(self):
        client = _FakeIMAPClient(capabilities_error=OSError("capability lookup failed"))

        send_client_id(client, _mailbox())  # 不抛异常

        assert client.id_calls == []

    def test_id_failure_is_logged_but_never_raised(self, caplog):
        client = _FakeIMAPClient(id_error=RuntimeError("ID not accepted"))

        with caplog.at_level(logging.WARNING):
            send_client_id(client, _mailbox())  # 不抛异常

        assert "发送 ID 失败" in caplog.text


# ------------------------------------------------------------- connect_imap
class TestConnectImapClientId:
    """`connect_imap` 与客户端 ID 的衔接（集成断言，不连真实服务器）。"""

    def test_client_id_sent_after_successful_login(self, monkeypatch):
        client = _FakeIMAPClient(supports_id=True, enforce_login_order=True)
        monkeypatch.setattr("imapclient.IMAPClient", lambda *a, **kw: client)

        conn = connect_imap(_mailbox(), "auth-code")

        assert conn is client
        assert client.login_calls == [("user@example.com", "auth-code")]
        assert client.id_calls, "登录后未发送 ID：网易等 Coremail 平台会拒绝 SELECT"

    def test_connection_still_usable_without_id_support(self, monkeypatch):
        """不支持 ID 的服务商（飞书 / Gmail 等）不得受影响。"""
        client = _FakeIMAPClient(supports_id=False)
        monkeypatch.setattr("imapclient.IMAPClient", lambda *a, **kw: client)

        conn = connect_imap(_mailbox(), "auth-code")

        assert conn is client
        assert client.login_calls == [("user@example.com", "auth-code")]
        assert client.id_calls == []

    def test_login_failure_does_not_attempt_id(self, monkeypatch):
        """登录都失败时不应再发 ID（保持原有报错语义）。"""

        class _RejectingClient(_FakeIMAPClient):
            def login(self, username, secret):
                super().login(username, secret)
                raise RuntimeError("LOGIN Login error or password error")

        client = _RejectingClient()
        monkeypatch.setattr("imapclient.IMAPClient", lambda *a, **kw: client)

        with pytest.raises(Exception) as excinfo:
            connect_imap(_mailbox(), "bad-secret")

        assert "IMAP 登录失败" in str(excinfo.value)
        assert client.id_calls == []
