"""ntfy 发布客户端测试（契约 §4.1）。

**全部打桩**：不碰真实网络（`tests/conftest.py` 的既定原则：测试在离线环境运行）。
断言的是**请求构造**（URL / header / body），以及三条硬性要求：
超时存在、失败不抛异常、日志不含 token 明文。
"""

from __future__ import annotations

from urllib import error

import pytest

from apps.audit.models import Setting
from apps.notifications import ntfy

pytestmark = pytest.mark.django_db

SERVER = "https://ntfy.example.com"


class FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def captured(monkeypatch):
    """拦下 urlopen，记录每次请求。"""
    calls: list[dict] = []

    def fake_urlopen(req, timeout=None):
        calls.append(
            {
                "url": req.full_url,
                "method": req.method,
                "headers": {k.lower(): v for k, v in dict(req.headers).items()},
                "data": req.data,
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


@pytest.fixture
def ntfy_on():
    Setting.set("ntfy_enabled", "true")
    Setting.set("ntfy_server_url", SERVER)
    return SERVER


class TestConfiguration:
    def test_disabled_by_default(self):
        """默认关：未配置就不该发，避免装完就报错（契约 §6.1）。"""
        assert ntfy.is_enabled() is False

    def test_enabled_requires_both_flag_and_server(self):
        Setting.set("ntfy_enabled", "true")
        assert ntfy.is_enabled() is False  # 还没填 server
        Setting.set("ntfy_server_url", SERVER)
        assert ntfy.is_enabled() is True

    def test_server_url_trailing_slash_stripped(self):
        Setting.set("ntfy_server_url", f"{SERVER}/")
        assert ntfy.server_url() == SERVER

    def test_aggregate_seconds_guards_non_positive(self):
        """窗口必须为正 —— 0 或负数会退化成「每纳秒一个桶」，比不聚合还糟。"""
        Setting.set("notify_aggregate_seconds", "0")
        assert ntfy.aggregate_seconds() == 60
        Setting.set("notify_aggregate_seconds", "-5")
        assert ntfy.aggregate_seconds() == 60
        Setting.set("notify_aggregate_seconds", "30")
        assert ntfy.aggregate_seconds() == 30


class TestTokenStorage:
    def test_token_roundtrip(self):
        ntfy.set_token("tk_secret_value")
        assert ntfy.get_token() == "tk_secret_value"

    def test_token_is_encrypted_at_rest(self):
        """决策 D4：token 必须**密文**落库，不能明文。"""
        ntfy.set_token("tk_plaintext_probe")
        raw = Setting.get("ntfy_token")
        assert "tk_plaintext_probe" not in raw
        assert raw  # 密文非空

    def test_empty_token_clears(self):
        ntfy.set_token("tk_x")
        ntfy.set_token("")
        assert ntfy.get_token() == ""

    def test_missing_token_returns_empty(self):
        assert ntfy.get_token() == ""

    def test_undecryptable_token_degrades_to_no_auth(self, caplog):
        """FERNET_KEY 换过之后，坏密文不能炸掉推送 —— 降级为「无令牌」。"""
        Setting.set("ntfy_token", "not-a-valid-fernet-token")
        assert ntfy.get_token() == ""


class TestPublishRequest:
    def test_posts_to_server_and_topic(self, captured, ntfy_on):
        result = ntfy.publish("oridesk-abc", title="T#1 有新来信", message="主题")
        assert result.ok is True
        assert captured[0]["url"] == f"{SERVER}/oridesk-abc"
        assert captured[0]["method"] == "POST"

    def test_sets_title_priority_tags(self, captured, ntfy_on):
        ntfy.publish(
            "t1", title="T#1 有新来信", message="主题", priority="high", tags="a,b"
        )
        h = captured[0]["headers"]
        assert h["title"] == "T#1 有新来信"
        assert h["priority"] == "high"
        assert h["tags"] == "a,b"

    def test_sets_click_and_actions(self, captured, ntfy_on):
        ntfy.publish(
            "t1",
            title="x",
            message="y",
            click="oridesk://ticket/7",
            actions="view, 在浏览器打开, https://x/tickets/7/, clear=true",
        )
        h = captured[0]["headers"]
        assert h["click"] == "oridesk://ticket/7"
        assert h["actions"].startswith("view, 在浏览器打开")

    def test_sequence_id_header_for_aggregation(self, captured, ntfy_on):
        """聚合靠 X-Sequence-ID 让客户端**替换**前一条，而不是再堆一条。"""
        ntfy.publish("t1", title="x", message="y", sequence_id="agg_123")
        assert captured[0]["headers"]["x-sequence-id"] == "agg_123"

    def test_omits_optional_headers_when_absent(self, captured, ntfy_on):
        ntfy.publish("t1", title="x", message="y")
        h = captured[0]["headers"]
        assert "click" not in h
        assert "actions" not in h
        assert "x-sequence-id" not in h
        assert "authorization" not in h

    def test_body_is_utf8_and_truncated(self, captured, ntfy_on):
        ntfy.publish("t1", title="x", message="主" * 500)
        body = captured[0]["data"].decode("utf-8")
        assert len(body) == ntfy.MAX_BODY

    def test_sets_timeout(self, captured, ntfy_on):
        """必须设超时：一个卡住的 ntfy 会拖死收信循环。"""
        ntfy.publish("t1", title="x", message="y")
        assert captured[0]["timeout"] == ntfy.DEFAULT_TIMEOUT

    def test_bearer_token_sent_when_configured(self, captured, ntfy_on):
        """访问令牌对**发布**有效；订阅侧同样用它（契约 §4.3）。"""
        ntfy.set_token("tk_abc123")
        ntfy.publish("t1", title="x", message="y")
        assert captured[0]["headers"]["authorization"] == "Bearer tk_abc123"


class TestFailureHandling:
    """契约 §4.1：**绝不抛异常**，推送失败不能影响收信流水线。"""

    def test_missing_server_returns_failure_without_network(self, captured):
        result = ntfy.publish("t1", title="x", message="y")
        assert result.ok is False
        assert "ntfy_server_url" in result.error
        assert captured == []

    def test_empty_topic_returns_failure(self, captured, ntfy_on):
        result = ntfy.publish("", title="x", message="y")
        assert result.ok is False
        assert captured == []

    def test_http_error_is_swallowed(self, captured, ntfy_on, monkeypatch):
        def boom(req, timeout=None):
            raise error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", boom)
        result = ntfy.publish("t1", title="x", message="y")
        assert result.ok is False
        assert result.status == 401

    def test_network_error_is_swallowed(self, captured, ntfy_on, monkeypatch):
        def boom(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        result = ntfy.publish("t1", title="x", message="y")
        assert result.ok is False
        assert "connection refused" in result.error

    def test_non_2xx_status_is_failure(self, captured, ntfy_on, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda req, timeout=None: FakeResponse(500)
        )
        result = ntfy.publish("t1", title="x", message="y")
        assert result.ok is False
        assert result.status == 500

    def test_token_never_appears_in_logs(self, ntfy_on, monkeypatch):
        """安全：日志里绝不能出现 token 明文。

        这里不用 `caplog`：`config/settings.py` 给 `apps` logger 设了
        `propagate: False`，记录不会冒泡到 root，caplog 抓不到。
        改为直接往该 logger 挂一个临时 handler。
        """
        import logging

        ntfy.set_token("tk_super_secret_probe")
        captured_records: list[str] = []

        class _Collect(logging.Handler):
            def emit(self, record):
                captured_records.append(record.getMessage())

        handler = _Collect()
        target = logging.getLogger("apps.notifications.ntfy")
        target.addHandler(handler)
        try:

            def boom(req, timeout=None):
                raise OSError("refused")

            monkeypatch.setattr("urllib.request.urlopen", boom)
            result = ntfy.publish("t1", title="x", message="y")
        finally:
            target.removeHandler(handler)

        assert result.ok is False
        assert captured_records, "应当记了一条失败日志"
        assert all("tk_super_secret_probe" not in line for line in captured_records)
