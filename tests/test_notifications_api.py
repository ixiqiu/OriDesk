"""移动端端点 E1–E6 测试（契约 §3）。

重点覆盖那些**写错了也不会报错、只会在 App 上表现为怪现象**的地方：

- 未登录必须 **401 JSON**，不能是 302 跳登录页（否则 App 拿 HTML 当 JSON 解析）
- 非本人的订阅必须 **404**，不能是 403（403 会泄露「这个 id 存在」）
- CSRF 失败要返回 **机器可读**的 `csrf_failed`
- 响应里**绝不能出现 ntfy token**
- 角标数字必须与收件箱 chips **同一口径**（决策 D5）
"""

from __future__ import annotations

import json
import re

import pytest
from django.test import Client

from apps.audit.models import Setting
from apps.notifications import ntfy
from apps.notifications.models import Subscription
from apps.tickets.selectors import scope_counts
from tests.conftest import make_ticket

pytestmark = pytest.mark.django_db

BADGE = "/api/mobile/badge/"
SUBS = "/api/mobile/subscriptions/"
SERVER = "https://ntfy.example.com"


def detail_url(pk):
    return f"/api/mobile/subscriptions/{pk}/"


def push_url(pk):
    return f"/api/mobile/subscriptions/{pk}/test/"


@pytest.fixture
def api(client, tech_user):
    client.force_login(tech_user)
    return client


@pytest.fixture
def device(tech_user):
    return Subscription.objects.create(
        user=tech_user, topic="oridesk-abc", server=SERVER, device_label="Pixel 7"
    )


def post_json(client, url, payload, **kwargs):
    return client.post(
        url, data=json.dumps(payload), content_type="application/json", **kwargs
    )


def patch_json(client, url, payload):
    return client.patch(
        url, data=json.dumps(payload), content_type="application/json"
    )


class TestAuthContract:
    def test_badge_returns_401_not_302_when_anonymous(self, client):
        """**契约 §2.2 的核心**：302 会让 App 把登录页 HTML 当 JSON 解析。"""
        resp = client.get(BADGE)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    @pytest.mark.parametrize("method,url", [
        ("get", SUBS),
        ("post", SUBS),
        ("patch", "/api/mobile/subscriptions/1/"),
        ("delete", "/api/mobile/subscriptions/1/"),
        ("post", "/api/mobile/subscriptions/1/test/"),
    ])
    def test_every_endpoint_returns_401_json_when_anonymous(self, client, method, url):
        resp = getattr(client, method)(url)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_authenticated_can_read_badge(self, api):
        assert api.get(BADGE).status_code == 200


class TestBadge:
    def test_shape(self, api):
        body = api.get(BADGE).json()
        assert set(body) == {"awaiting", "unassigned", "mine", "all", "generated_at"}

    def test_matches_scope_counts_exactly(
        self, api, tech_user, tech_group, tech_mailbox
    ):
        """决策 D5：角标与收件箱 chips 必须**逐字同口径**。

        另写一套统计就会出现"角标显示 3、点进去 2 条"。
        """
        make_ticket(mailbox=tech_mailbox, group=tech_group, is_awaiting_reply=True)
        make_ticket(mailbox=tech_mailbox, group=tech_group, assignee=tech_user)
        make_ticket(
            mailbox=tech_mailbox,
            group=tech_group,
            status="closed",
            is_awaiting_reply=True,
        )
        expected = scope_counts(tech_user)
        body = api.get(BADGE).json()
        assert body["awaiting"] == expected["awaiting"]
        assert body["unassigned"] == expected["unassigned"]
        assert body["mine"] == expected["mine"]
        assert body["all"] == expected["all"]

    def test_closed_ticket_not_counted_as_awaiting(
        self, api, tech_group, tech_mailbox
    ):
        make_ticket(
            mailbox=tech_mailbox,
            group=tech_group,
            status="closed",
            is_awaiting_reply=True,
        )
        assert api.get(BADGE).json()["awaiting"] == 0

    def test_sets_csrf_cookie(self, api):
        """决策 D9：E1 顺带下发 csrftoken，App 靠它做后续写操作。"""
        resp = api.get(BADGE)
        assert "csrftoken" in resp.cookies

    def test_rejects_post(self, api):
        assert api.post(BADGE).status_code == 405


class TestSubscriptionList:
    def test_only_own_subscriptions(self, api, tech_user, finance_user, device):
        Subscription.objects.create(user=finance_user, topic="other-topic")
        body = api.get(SUBS).json()
        assert [s["id"] for s in body["subscriptions"]] == [device.pk]

    def test_never_exposes_token(self, api, device):
        ntfy.set_token("tk_must_not_leak")
        raw = api.get(SUBS).content.decode()
        assert "tk_must_not_leak" not in raw
        assert "token" not in raw.lower()

    def test_shape_has_no_user_field(self, api, device):
        body = api.get(SUBS).json()["subscriptions"][0]
        assert "user" not in body
        assert set(body) == {
            "id", "topic", "server", "device_label", "enabled",
            "dnd_start", "dnd_end", "last_seen_at", "created_at",
        }


class TestSubscriptionCreate:
    def test_creates_with_server_generated_topic(self, api):
        resp = post_json(api, SUBS, {"device_label": "Pixel 7"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["topic"].startswith("oridesk-")
        assert body["device_label"] == "Pixel 7"

    def test_topic_is_ntfy_compatible_and_unguessable(self, api):
        """topic 本质是密码：必须够长够随机，且字符集落在 ntfy 允许范围内。"""
        topic = post_json(api, SUBS, {}).json()["topic"]
        assert re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic)
        assert len(topic.split("-", 1)[1]) == 32

    def test_two_devices_get_different_topics(self, api):
        a = post_json(api, SUBS, {"device_label": "A"}).json()["topic"]
        b = post_json(api, SUBS, {"device_label": "B"}).json()["topic"]
        assert a != b

    def test_seeds_last_seen_at(self, api):
        assert post_json(api, SUBS, {}).json()["last_seen_at"] is not None

    def test_records_current_server(self, api):
        Setting.set("ntfy_server_url", SERVER)
        assert post_json(api, SUBS, {}).json()["server"] == SERVER

    def test_echoed_topic_upserts_without_duplicate(self, api):
        """契约 §3.3 幂等：App 重试/重装后续订，不能每次都多一条记录。"""
        first = post_json(api, SUBS, {"device_label": "Pixel 7"}).json()
        again = post_json(api, SUBS, {"topic": first["topic"], "device_label": "Pixel 8"})
        assert again.status_code == 200
        assert again.json()["id"] == first["id"]
        assert again.json()["topic"] == first["topic"]
        assert again.json()["device_label"] == "Pixel 8"
        assert Subscription.objects.count() == 1

    def test_echoed_topic_refreshes_last_seen(self, api):
        first = post_json(api, SUBS, {}).json()
        assert first["last_seen_at"] is not None
        again = post_json(api, SUBS, {"topic": first["topic"]}).json()
        assert again["last_seen_at"] >= first["last_seen_at"]

    def test_echoed_topic_of_another_user_is_404(self, api, finance_user):
        """不能靠回显别人的 topic 来窃取频道。"""
        foreign = Subscription.objects.create(user=finance_user, topic="foreign-topic")
        resp = post_json(api, SUBS, {"topic": foreign.topic})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_cannot_dictate_arbitrary_topic(self, api):
        """App 不能自造 topic —— 只允许回显服务端已下发的。"""
        resp = post_json(api, SUBS, {"topic": "i-picked-this"})
        assert resp.status_code == 404
        assert not Subscription.objects.filter(topic="i-picked-this").exists()

    def test_does_not_reenable_disabled_device(self, api, device):
        """用户主动关掉的推送，不该被一次续订悄悄改回来。"""
        device.enabled = False
        device.save(update_fields=["enabled"])
        again = post_json(api, SUBS, {"topic": device.topic}).json()
        assert again["enabled"] is False


class TestSubscriptionUpdate:
    def test_patch_enabled(self, api, device):
        resp = patch_json(api, detail_url(device.pk), {"enabled": False})
        assert resp.status_code == 200
        device.refresh_from_db()
        assert device.enabled is False

    def test_string_false_does_not_enable(self, api, device):
        """`bool("false")` 是 True —— 必须按字面解析，否则"关闭推送"会变成"打开"。"""
        device.enabled = False
        device.save(update_fields=["enabled"])
        patch_json(api, detail_url(device.pk), {"enabled": "false"})
        device.refresh_from_db()
        assert device.enabled is False

    def test_patch_dnd_window(self, api, device):
        resp = patch_json(
            api, detail_url(device.pk), {"dnd_start": "22:00", "dnd_end": "08:00"}
        )
        body = resp.json()
        assert body["dnd_start"] == "22:00"
        assert body["dnd_end"] == "08:00"
        device.refresh_from_db()
        assert device.dnd_enabled is True

    def test_dnd_can_be_cleared_with_null(self, api, device):
        patch_json(api, detail_url(device.pk), {"dnd_start": "22:00", "dnd_end": "08:00"})
        body = patch_json(api, detail_url(device.pk), {"dnd_start": None}).json()
        assert body["dnd_start"] is None

    def test_bad_time_is_validation_error(self, api, device):
        resp = patch_json(api, detail_url(device.pk), {"dnd_start": "25:99"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "validation_error"

    def test_topic_is_immutable(self, api, device):
        """topic 改了等于静默换频道，必须走吊销+重注册。"""
        resp = patch_json(api, detail_url(device.pk), {"topic": "new-topic"})
        assert resp.status_code == 400
        device.refresh_from_db()
        assert device.topic == "oridesk-abc"

    def test_foreign_subscription_is_404_not_403(self, api, finance_user):
        foreign = Subscription.objects.create(user=finance_user, topic="foreign")
        resp = patch_json(api, detail_url(foreign.pk), {"enabled": False})
        assert resp.status_code == 404

    def test_nonexistent_is_404(self, api):
        assert patch_json(api, detail_url(999999), {"enabled": False}).status_code == 404


class TestSubscriptionDelete:
    def test_deletes_own(self, api, device):
        resp = api.delete(detail_url(device.pk))
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True}
        assert not Subscription.objects.filter(pk=device.pk).exists()

    def test_foreign_is_404(self, api, finance_user):
        foreign = Subscription.objects.create(user=finance_user, topic="foreign")
        assert api.delete(detail_url(foreign.pk)).status_code == 404
        assert Subscription.objects.filter(pk=foreign.pk).exists()

    def test_repeat_delete_is_404(self, api, device):
        assert api.delete(detail_url(device.pk)).status_code == 200
        assert api.delete(detail_url(device.pk)).status_code == 404

    def test_delete_is_scoped_to_owner(self, api, tech_user, finance_user):
        """吊销只影响本人的记录，不能借 id 删别人的。"""
        mine = Subscription.objects.create(user=tech_user, topic="mine")
        theirs = Subscription.objects.create(user=finance_user, topic="theirs")
        api.delete(detail_url(mine.pk))
        assert Subscription.objects.filter(pk=theirs.pk).exists()


class TestSubscriptionTestPush:
    def test_reports_success(self, api, device, monkeypatch):
        monkeypatch.setattr(
            ntfy, "publish", lambda topic, **kw: ntfy.PublishResult(ok=True, status=200)
        )
        body = api.post(push_url(device.pk)).json()
        assert body == {"sent": True, "mode": "direct"}

    def test_reports_failure_without_500(self, api, device, monkeypatch):
        monkeypatch.setattr(
            ntfy, "publish",
            lambda topic, **kw: ntfy.PublishResult(ok=False, error="HTTP 401"),
        )
        resp = api.post(push_url(device.pk))
        assert resp.status_code == 200
        assert resp.json()["sent"] is False
        assert "HTTP 401" in resp.json()["error"]

    def test_targets_the_subscription_topic(self, api, device, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            ntfy, "publish",
            lambda topic, **kw: (seen.update(topic=topic), ntfy.PublishResult(ok=True))[1],
        )
        api.post(push_url(device.pk))
        assert seen["topic"] == device.topic

    def test_works_before_ntfy_enabled(self, api, device, monkeypatch):
        """E6 的全部价值就是"启用前先验链路"，所以不能被总开关挡住。"""
        assert ntfy.is_enabled() is False
        monkeypatch.setattr(
            ntfy, "publish", lambda topic, **kw: ntfy.PublishResult(ok=True)
        )
        assert api.post(push_url(device.pk)).json()["sent"] is True

    def test_foreign_subscription_is_404(self, api, finance_user):
        foreign = Subscription.objects.create(user=finance_user, topic="foreign")
        assert api.post(push_url(foreign.pk)).status_code == 404


class TestCsrfAndMethods:
    def test_post_without_csrf_token_is_json_403(self, tech_user):
        """契约 §2.3：CSRF 失败必须是机器可读的，App 才能"取 cookie 后重试一次"。"""
        c = Client(enforce_csrf_checks=True)
        c.force_login(tech_user)
        resp = post_json(c, SUBS, {"device_label": "x"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_failed"

    def test_csrf_failure_keeps_html_for_web_paths(self, rf):
        """不能为了 App 把 Web 的 403 页面弄丢。"""
        from apps.core.views import csrf_failure

        resp = csrf_failure(rf.post("/tickets/1/reply/"), "reason")
        assert resp.status_code == 403
        assert b"<" in resp.content

    def test_post_with_csrf_token_succeeds(self, tech_user):
        c = Client(enforce_csrf_checks=True)
        c.force_login(tech_user)
        c.get(BADGE)  # 取 csrftoken cookie（决策 D9）
        token = c.cookies["csrftoken"].value
        resp = c.post(
            SUBS,
            data=json.dumps({}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        assert resp.status_code == 201

    def test_put_is_405(self, api):
        assert api.put(SUBS, data="{}", content_type="application/json").status_code == 405

    def test_malformed_json_is_bad_request(self, api):
        resp = api.post(SUBS, data="{not json", content_type="application/json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "bad_request"

    def test_json_array_body_is_bad_request(self, api):
        resp = api.post(SUBS, data="[1,2]", content_type="application/json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "bad_request"
