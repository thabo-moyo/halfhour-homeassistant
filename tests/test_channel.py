"""The MQTT channel: connect, presence, plans, commands, backoff, reauth."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.halfhour.channel import HalfhourChannel, url_allowed
from custom_components.halfhour.const import CONF_HUB_ID, CONF_TOKEN, CONF_URL, DOMAIN
from custom_components.halfhour.plan import Plan

from .fakes import FakeTransport

HUB = "hub-1"
TOKEN = "secret-device-token"
PLAN = {
    "v": 1,
    "id": "run-1",
    "account": "acct",
    "made_at": "2026-09-28T12:00:00Z",
    "slot_minutes": 30,
    "slots": [
        {"start": "2026-09-28T12:00:00Z", "import_p": 7.0, "export_p": 15.0, "load_w": 400, "pv_w": 0, "battery_w": -500, "grid_w": 900, "soc": 60}
    ],
}


def make_entry(hass: HomeAssistant) -> MockConfigEntry:
    e = MockConfigEntry(domain=DOMAIN, unique_id=HUB, data={CONF_URL: "https://hh.test", CONF_TOKEN: TOKEN, CONF_HUB_ID: HUB})
    e.add_to_hass(hass)
    return e


class Harness:
    def __init__(self, hass: HomeAssistant, url: str = "mqtt://192.168.8.200:1883", account: str | None = "acct", fail: Exception | None = None) -> None:
        self.hass = hass
        self.entry = make_entry(hass)
        self.transport = FakeTransport()
        self.plans: list[Plan] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self.key_ok: bool | Exception = True  # what checking the key over HTTP finds
        self.gate: asyncio.Event | None = None  # when set, the key check waits for it
        self.verifies = 0
        self.device_lists: list[bytes] = []
        self.channel = HalfhourChannel(
            hass, self.entry, url, account, self.plans.append, self._command, self._verify, on_devices=self.device_lists.append, transport=self.transport
        )

    async def _verify(self) -> bool:
        self.verifies += 1
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.key_ok, Exception):
            raise self.key_ok
        return self.key_ok

    async def _command(self, name: str, args: dict[str, Any]) -> None:
        self.commands.append((name, args))
        if self.fail is not None:
            raise self.fail

    async def start(self) -> None:
        await self.channel.async_start()
        await self.hass.async_block_till_done()

    async def connected(self) -> None:
        await self.start()
        self.transport.on_connect(0)
        await self.hass.async_block_till_done()

    async def message(self, topic: str, payload: Any) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.transport.on_message(topic, raw)
        await self.hass.async_block_till_done(wait_background_tasks=True)


# -- url_allowed ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("mqtt://192.168.8.200:1883", True),
        ("mqtt://10.0.0.5", True),
        ("mqtt://172.16.1.1", True),
        ("mqtt://169.254.3.4", True),
        ("mqtt://homeassistant.local", True),
        ("ws://192.168.1.2:8083/mqtt", True),
        ("mqtt://example.com", False),
        ("mqtt://8.8.8.8:1883", False),
        ("ws://example.com/mqtt", False),
        ("mqtts://example.com:8883", True),
        ("wss://halfhour.energy/mqtt", True),
        ("http://192.168.1.2", False),
        ("mqtt://", False),
        ("not a url", False),
        ("mqtt://[::1", False),
        ("mqtt://10.0.0.1:99999", False),
        ("mqtts://example.com:port", False),
        ("mqtt://[fe80::1]:1883", True),
    ],
)
def test_url_allowed(url: str, ok: bool) -> None:
    assert url_allowed(url) is ok


# -- connecting -----------------------------------------------------------------


async def test_start_connects_as_the_hub_with_a_retained_offline_will(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.start()
    [c] = h.transport.connects
    assert (c["host"], c["port"], c["tls"], c["ws"]) == ("192.168.8.200", 1883, False, False)
    assert (c["username"], c["password"], c["keepalive"]) == (HUB, TOKEN, 60)
    topic, payload = c["will"]
    will = json.loads(payload)
    assert topic == f"homes/{HUB}/status"
    assert will["v"] == 1 and will["state"] == "offline" and will["since"] and will["version"]
    assert h.channel.live is False


@pytest.mark.parametrize(
    ("url", "expect"),
    [
        ("mqtts://broker.example:8883", ("broker.example", 8883, True, False, "/mqtt")),
        ("mqtts://broker.example", ("broker.example", 8883, True, False, "/mqtt")),
        ("wss://broker.example/ws", ("broker.example", 443, True, True, "/ws")),
        ("ws://10.1.2.3:8083", ("10.1.2.3", 8083, False, True, "/mqtt")),
        ("mqtt://10.1.2.3", ("10.1.2.3", 1883, False, False, "/mqtt")),
    ],
)
async def test_the_url_picks_tls_websockets_and_port(hass: HomeAssistant, url: str, expect: tuple[Any, ...]) -> None:
    h = Harness(hass, url=url)
    await h.start()
    c = h.transport.connects[0]
    assert (c["host"], c["port"], c["tls"], c["ws"], c["path"]) == expect


async def test_on_connect_publishes_online_then_subscribes(hass: HomeAssistant) -> None:
    h = Harness(hass)
    seen: list[bool] = []
    h.channel.add_listener(lambda: seen.append(h.channel.live))
    await h.connected()
    [(topic, payload, qos, retain)] = h.transport.published
    online = json.loads(payload)
    will = json.loads(h.transport.connects[0]["will"][1])
    assert (topic, qos, retain) == (f"homes/{HUB}/status", 1, True)
    assert online["state"] == "online" and online["v"] == 1
    assert online["since"] == will["since"]  # one connection, one since: the will never looks newer
    assert h.transport.subscribed == [[(f"homes/{HUB}/config", 1), (f"homes/{HUB}/cmd", 1), (f"homes/{HUB}/devices", 1), ("accounts/acct/plan", 1)]]
    assert h.channel.live is True and seen == [True]


async def test_no_plan_topic_without_an_account(hass: HomeAssistant) -> None:
    h = Harness(hass, account=None)
    await h.connected()
    assert h.transport.subscribed == [[(f"homes/{HUB}/config", 1), (f"homes/{HUB}/cmd", 1), (f"homes/{HUB}/devices", 1)]]


async def test_listener_can_be_removed(hass: HomeAssistant) -> None:
    h = Harness(hass)
    seen: list[bool] = []
    remove = h.channel.add_listener(lambda: seen.append(True))
    remove()
    await h.connected()
    assert seen == []


async def test_the_token_is_never_logged(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    h = Harness(hass)
    caplog.set_level("DEBUG")
    await h.connected()
    h.transport.on_disconnect(7)
    with patch.object(h.entry, "async_start_reauth"):
        h.transport.on_connect(135)
    await hass.async_block_till_done()
    assert TOKEN not in caplog.text


# -- plans --------------------------------------------------------------------


async def test_a_plan_reaches_on_plan(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    await h.message("accounts/acct/plan", PLAN)
    [plan] = h.plans
    assert plan.id == "run-1" and plan.made_at == datetime(2026, 9, 28, 12, tzinfo=UTC)


async def test_a_bad_plan_is_logged_once_per_id_and_ignored(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    h = Harness(hass)
    await h.connected()
    bad = {**PLAN, "slot_minutes": 7}
    await h.message("accounts/acct/plan", bad)
    await h.message("accounts/acct/plan", bad)
    await h.message("accounts/acct/plan", b"not json")
    assert h.plans == []
    assert caplog.text.count("run-1") == 1
    assert caplog.text.count("Ignoring a bad plan") == 2


# -- commands -----------------------------------------------------------------


async def test_a_command_runs_once_and_is_acked(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    await h.message(f"homes/{HUB}/cmd", {"v": 1, "id": "c1", "name": "resync", "args": {"x": 1}})
    assert h.commands == [("resync", {"x": 1})]
    assert h.transport.sent(f"homes/{HUB}/ack") == [{"v": 1, "id": "c1", "ok": True}]
    ack = [p for p in h.transport.published if p[0] == f"homes/{HUB}/ack"][0]
    assert (ack[2], ack[3]) == (1, False)
    assert h.channel.command_ids == ["c1"]


async def test_a_duplicate_command_is_acked_again_but_not_run(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    cmd = {"v": 1, "id": "c1", "name": "resync"}
    await h.message(f"homes/{HUB}/cmd", cmd)
    await h.message(f"homes/{HUB}/cmd", cmd)
    assert h.commands == [("resync", {})]
    assert h.transport.sent(f"homes/{HUB}/ack") == [{"v": 1, "id": "c1", "ok": True}] * 2


async def test_a_duplicate_while_the_first_still_runs_is_not_run_or_acked_twice(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    release = asyncio.Event()
    run = h._command

    async def slow(name: str, args: dict[str, Any]) -> None:
        await release.wait()
        await run(name, args)

    h.channel._on_command = slow
    cmd = json.dumps({"v": 1, "id": "c1", "name": "resync"}).encode()
    h.transport.on_message(f"homes/{HUB}/cmd", cmd)
    h.transport.on_message(f"homes/{HUB}/cmd", cmd)
    await hass.async_block_till_done()
    assert h.transport.sent(f"homes/{HUB}/ack") == []
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert h.commands == [("resync", {})]
    assert h.transport.sent(f"homes/{HUB}/ack") == [{"v": 1, "id": "c1", "ok": True}]


async def test_only_the_last_100_ids_are_remembered(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    for i in range(101):
        await h.message(f"homes/{HUB}/cmd", {"v": 1, "id": f"c{i}", "name": "resync"})
    await h.message(f"homes/{HUB}/cmd", {"v": 1, "id": "c0", "name": "resync"})
    assert len(h.commands) == 102  # c0 fell out of the window, so it runs again
    assert h.channel.command_ids == [f"c{i}" for i in range(92, 101)] + ["c0"]


async def test_a_failing_command_acks_not_ok(hass: HomeAssistant) -> None:
    h = Harness(hass, fail=RuntimeError("boom"))
    await h.connected()
    await h.message(f"homes/{HUB}/cmd", {"v": 1, "id": "c1", "name": "resync"})
    assert h.transport.sent(f"homes/{HUB}/ack") == [{"v": 1, "id": "c1", "ok": False, "error": "boom"}]


@pytest.mark.parametrize(
    "payload",
    [b"nope", json.dumps([1]).encode(), json.dumps({"v": 1, "name": "resync"}).encode(), json.dumps({"v": 1, "id": "c1"}).encode(), json.dumps({"v": 1, "id": "c1", "name": "x", "args": [1]}).encode()],
)
async def test_a_malformed_command_is_not_run(hass: HomeAssistant, payload: bytes) -> None:
    h = Harness(hass)
    await h.connected()
    await h.message(f"homes/{HUB}/cmd", payload)
    assert h.commands == []
    acks = h.transport.sent(f"homes/{HUB}/ack")
    if b'"id": "c1"' in payload:
        assert acks == [{"v": 1, "id": "c1", "ok": False, "error": "malformed command"}]
    else:
        assert acks == []


async def test_config_goes_to_reload_config(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    config = {"v": 1, "roles": [], "presets": {}, "stale_after_s": 5400}
    await h.message(f"homes/{HUB}/config", config)
    assert h.commands == [("reload_config", config)]
    assert h.transport.sent(f"homes/{HUB}/ack") == []


async def test_bad_config_or_a_failing_reload_is_logged(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    h = Harness(hass, fail=RuntimeError("boom"))
    await h.connected()
    await h.message(f"homes/{HUB}/config", b"{")
    await h.message(f"homes/{HUB}/config", {"v": 1})
    assert h.commands == [("reload_config", {"v": 1})]
    assert "bad config" in caplog.text and "boom" in caplog.text


async def test_the_device_list_reaches_on_devices_raw(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    await h.message(f"homes/{HUB}/devices", b'{"v": 1}')
    assert h.device_lists == [b'{"v": 1}']
    assert h.commands == [] and h.plans == []


async def test_a_message_on_another_topic_is_ignored(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    await h.message("somewhere/else", {"v": 1})
    assert h.commands == [] and h.plans == []


# -- auth and reconnects ------------------------------------------------------


@pytest.mark.parametrize("code", [4, 5, 134, 135])
async def test_not_authorised_with_a_refused_key_starts_reauth_once_and_stops(hass: HomeAssistant, code: int) -> None:
    h = Harness(hass)
    h.key_ok = False
    await h.start()
    with patch.object(h.entry, "async_start_reauth") as reauth, patch("custom_components.halfhour.channel.async_call_later") as later:
        h.transport.on_connect(code)
        h.transport.on_disconnect(code)
        await hass.async_block_till_done(wait_background_tasks=True)
        h.transport.on_connect(code)
        await hass.async_block_till_done(wait_background_tasks=True)
    reauth.assert_called_once_with(hass)
    later.assert_not_called()
    assert h.verifies == 1
    assert h.channel.live is False


@pytest.mark.parametrize("key_ok", [True, RuntimeError("gateway unreachable")])
@pytest.mark.parametrize("code", [4, 5, 134, 135])
async def test_not_authorised_while_the_key_still_works_is_an_outage(hass: HomeAssistant, code: int, key_ok: bool | Exception) -> None:
    """EMQX says 135 when its auth hook is down: that must not put the home into reauth."""
    h = Harness(hass)
    h.key_ok = key_ok
    await h.connected()
    delays: list[float] = []
    with patch.object(h.entry, "async_start_reauth") as reauth, record_delays(delays), patch("custom_components.halfhour.channel.random.uniform", return_value=1.0):
        h.transport.on_disconnect(7)
        h.channel._reconnect_unsub = None  # as if the timer had fired
        h.transport.on_connect(code)
        h.transport.on_disconnect(code)  # paho closes too: still one retry
        await hass.async_block_till_done(wait_background_tasks=True)
    reauth.assert_not_called()
    assert h.verifies == 1
    assert delays == [1.0, 60.0]  # the backoff would say 2 s; an outage waits at least a minute
    assert h.channel.live is False


async def test_a_stop_during_the_key_check_does_nothing_after(hass: HomeAssistant) -> None:
    h = Harness(hass)
    h.key_ok = False
    h.gate = asyncio.Event()
    await h.start()
    with patch.object(h.entry, "async_start_reauth") as reauth, patch("custom_components.halfhour.channel.async_call_later") as later:
        h.transport.on_connect(135)
        await asyncio.sleep(0)
        assert h.verifies == 1
        await h.channel.async_stop()
        h.gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    reauth.assert_not_called()
    later.assert_not_called()


async def test_a_refused_but_authorised_connect_retries(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.start()
    with patch("custom_components.halfhour.channel.async_call_later") as later:
        h.transport.on_connect(0x88)  # server unavailable
        h.transport.on_disconnect(0x88)  # paho closes too: still one retry
    assert later.call_count == 1


async def test_disconnects_back_off_with_jitter_capped_at_300(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    delays: list[float] = []
    with patch("custom_components.halfhour.channel.async_call_later", side_effect=lambda _h, d, _a: delays.append(d) or (lambda: None)):
        for _ in range(12):
            h.transport.on_disconnect(7)
            h.channel._reconnect_unsub = None  # as if the timer had fired
    assert h.channel.live is False
    bases = [min(300, 2**n) for n in range(12)]
    for delay, base in zip(delays, bases, strict=True):
        assert 0.7 * base <= delay <= 1.3 * base
    assert max(delays) <= 390


async def test_the_reconnect_timer_connects_again(hass: HomeAssistant, freezer) -> None:
    h = Harness(hass)
    await h.connected()
    with patch("custom_components.halfhour.channel.random.uniform", return_value=1.0):
        h.transport.on_disconnect(7)
        freezer.tick(2)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert len(h.transport.connects) == 2
    h.transport.on_connect(0)
    await hass.async_block_till_done()
    assert h.channel.live is True


def record_delays(delays: list[float]) -> Any:
    return patch("custom_components.halfhour.channel.async_call_later", side_effect=lambda _h, d, _a: delays.append(d) or (lambda: None))


async def test_accept_then_drop_keeps_backing_off(hass: HomeAssistant) -> None:
    """A broker that accepts and at once drops (say a denied subscribe) must not get 1 s reconnects forever."""
    h = Harness(hass)
    await h.start()
    delays: list[float] = []
    with patch("custom_components.halfhour.channel.random.uniform", return_value=1.0):
        for _ in range(4):
            h.transport.on_connect(0)
            with record_delays(delays):
                h.transport.on_disconnect(7)
            h.channel._reconnect_unsub = None  # as if the timer had fired
    assert delays == [1.0, 2.0, 4.0, 8.0]


async def test_a_connection_up_for_a_minute_resets_the_backoff(hass: HomeAssistant, freezer) -> None:
    h = Harness(hass)
    await h.start()
    delays: list[float] = []
    with patch("custom_components.halfhour.channel.random.uniform", return_value=1.0):
        for _ in range(3):
            h.transport.on_connect(0)
            with record_delays(delays):
                h.transport.on_disconnect(7)
            h.channel._reconnect_unsub = None
        h.transport.on_connect(0)
        freezer.tick(61)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        with record_delays(delays):
            h.transport.on_disconnect(7)
    assert delays == [1.0, 2.0, 4.0, 1.0]


async def test_a_refused_subscription_is_logged_by_topic(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    h = Harness(hass)
    await h.connected()
    h.transport.on_subscribe([1, 1, 1, 0x87])
    assert "accounts/acct/plan" in caplog.text and "135" in caplog.text
    assert f"homes/{HUB}/cmd" not in caplog.text


async def test_an_unreachable_broker_is_retried(hass: HomeAssistant) -> None:
    h = Harness(hass)
    h.transport.connect_error = OSError("refused")
    with patch("custom_components.halfhour.channel.async_call_later") as later:
        await h.start()
    assert later.call_count == 1 and h.channel.live is False


# -- stopping -------------------------------------------------------------------


async def test_stop_publishes_offline_and_disconnects(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    await h.channel.async_stop()
    assert h.transport.sent(f"homes/{HUB}/status")[-1]["state"] == "offline"
    assert h.transport.published[-1][3] is True  # retained
    assert h.transport.disconnects == 1
    assert h.channel.live is False
    h.transport.on_disconnect(0)  # the clean close: no reconnect
    assert h.channel._reconnect_unsub is None


async def test_stop_while_offline_cancels_the_retry_and_publishes_nothing(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    h.transport.on_disconnect(7)
    assert h.channel._reconnect_unsub is not None
    published = len(h.transport.published)
    await h.channel.async_stop()
    assert h.channel._reconnect_unsub is None
    assert len(h.transport.published) == published
    assert h.transport.disconnects == 1


async def test_a_connect_that_returns_after_stop_is_closed(hass: HomeAssistant) -> None:
    h = Harness(hass)
    real = h.transport.connect

    def connect_then_stop(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        h.channel._stopped = True

    h.transport.connect = connect_then_stop  # type: ignore[method-assign]
    await h.start()
    assert h.transport.disconnects == 1


# -- insecure brokers -----------------------------------------------------------


async def test_an_insecure_url_raises_a_repair_and_never_connects(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    h = Harness(hass, url="mqtt://example.com:1883")
    await h.start()
    assert h.transport.connects == []
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{h.entry.entry_id}_insecure_broker")
    assert issue is not None and issue.translation_key == "insecure_broker"
    assert issue.translation_placeholders == {"url": "mqtt://example.com:1883"}
    assert "insecure" in caplog.text.lower()


async def test_a_secure_url_clears_the_repair(hass: HomeAssistant) -> None:
    h = Harness(hass)
    ir.async_create_issue(hass, DOMAIN, f"{h.entry.entry_id}_insecure_broker", is_fixable=False, severity=ir.IssueSeverity.ERROR, translation_key="insecure_broker")
    await h.start()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{h.entry.entry_id}_insecure_broker") is None


# -- the paho transport ---------------------------------------------------------


async def test_paho_transport_connects_with_a_persistent_v5_session(hass: HomeAssistant) -> None:
    import paho.mqtt.client as mqtt
    from paho.mqtt.reasoncodes import ReasonCode

    from custom_components.halfhour.channel import PahoTransport

    with patch("paho.mqtt.client.Client") as cls:
        client = cls.return_value
        t = PahoTransport(hass, HUB)
        t.subscribe([("x", 1)])  # before any connect: nothing to do
        t.publish("x", b"", 1, False)
        await hass.async_add_executor_job(t.connect, "b.example", 443, True, True, HUB, TOKEN, ("homes/hub-1/status", b"off"), 60, "/ws")
    args, kwargs = cls.call_args
    assert args == (mqtt.CallbackAPIVersion.VERSION2,)
    assert kwargs["client_id"] == HUB and kwargs["protocol"] == mqtt.MQTTv5 and kwargs["transport"] == "websockets"
    assert kwargs["reconnect_on_failure"] is False
    client.ws_set_options.assert_called_once_with(path="/ws")
    client.tls_set.assert_called_once_with()
    client.username_pw_set.assert_called_once_with(HUB, TOKEN)
    client.will_set.assert_called_once_with("homes/hub-1/status", b"off", qos=1, retain=True)
    (host, port), ckw = client.connect.call_args
    assert (host, port, ckw["keepalive"], ckw["clean_start"]) == ("b.example", 443, 60, False)
    assert ckw["properties"].SessionExpiryInterval == 86400
    client.loop_start.assert_called_once_with()

    got: list[tuple[str, Any]] = []
    t.on_connect = lambda rc: got.append(("connect", rc))
    t.on_disconnect = lambda rc: got.append(("disconnect", rc))
    t.on_message = lambda topic, payload: got.append(("message", (topic, payload)))
    t.on_subscribe = lambda codes: got.append(("subscribe", codes))
    msg = mqtt.MQTTMessage(topic=b"homes/hub-1/cmd")
    msg.payload = b"{}"

    def from_paho_thread() -> None:
        client.on_connect(client, None, None, ReasonCode(2, identifier=135), None)
        client.on_disconnect(client, None, None, ReasonCode(14, identifier=0), None)
        client.on_message(client, None, msg)
        client.on_subscribe(client, None, 1, [ReasonCode(9, identifier=1), ReasonCode(9, identifier=0x87)], None)

    await hass.async_add_executor_job(from_paho_thread)
    await hass.async_block_till_done()
    assert got == [("connect", 135), ("disconnect", 0), ("message", ("homes/hub-1/cmd", b"{}")), ("subscribe", [1, 135])]

    t.subscribe([("a", 1)])
    t.publish("a", b"p", 1, True)
    client.subscribe.assert_called_once_with([("a", 1)])
    client.publish.assert_called_once_with("a", b"p", qos=1, retain=True)
    await hass.async_add_executor_job(t.disconnect)
    client.disconnect.assert_called_once_with()
    client.loop_stop.assert_called_once_with()
    assert client.on_connect is None and client.on_message is None and client.on_subscribe is None


async def test_paho_transport_plain_tcp(hass: HomeAssistant) -> None:
    from custom_components.halfhour.channel import PahoTransport

    with patch("paho.mqtt.client.Client") as cls:
        t = PahoTransport(hass, HUB)
        await hass.async_add_executor_job(t.connect, "10.0.0.2", 1883, False, False, HUB, TOKEN, ("s", b"off"), 60)
    assert cls.call_args.kwargs["transport"] == "tcp"
    cls.return_value.tls_set.assert_not_called()
    cls.return_value.ws_set_options.assert_not_called()


async def test_the_channel_builds_a_paho_transport_by_default(hass: HomeAssistant) -> None:
    from custom_components.halfhour.channel import PahoTransport

    entry = make_entry(hass)

    async def cmd(name: str, args: dict[str, Any]) -> None:
        return None

    async def verify() -> bool:
        return True

    channel = HalfhourChannel(hass, entry, "mqtt://10.0.0.2", None, lambda plan: None, cmd, verify)
    assert isinstance(channel._transport, PahoTransport)


async def test_messages_after_stop_are_ignored(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.connected()
    await h.channel.async_stop()
    await h.message(f"homes/{HUB}/cmd", {"id": "c1", "name": "resync", "args": {}})
    await h.message("accounts/acct/plan", PLAN)
    assert h.commands == [] and h.plans == []


async def test_live_going_down_and_up_is_logged_once_each(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    h = Harness(hass)
    await h.connected()
    caplog.clear()
    caplog.set_level(logging.INFO, logger="custom_components.halfhour.channel")
    with record_delays([]):
        h.transport.on_disconnect(7)
        for _ in range(3):
            h.channel._reconnect_unsub = None
            h.transport.on_connect(0x88)
            h.transport.on_disconnect(0x88)
    h.transport.connect_error = OSError("refused")
    with record_delays([]):
        h.channel._reconnect_unsub = None
        await h.channel._async_connect()
    infos = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert len(infos) == 1 and "lost" in infos[0].getMessage().lower()
    h.transport.on_connect(0)
    h.transport.on_disconnect(7)  # down again: a second, separate outage
    h.transport.on_connect(0)
    infos = [r.getMessage().lower() for r in caplog.records if r.levelno >= logging.INFO]
    assert len(infos) == 4
    assert "back" in infos[1] and "lost" in infos[2] and "back" in infos[3]


async def test_a_first_connect_that_fails_is_logged_once(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.halfhour.channel")
    h = Harness(hass)
    h.transport.connect_error = OSError("refused")
    with record_delays([]):
        await h.start()
        h.channel._reconnect_unsub = None
        await h.channel._async_connect()
    infos = [r for r in caplog.records if r.levelno >= logging.INFO and r.name.endswith("channel")]
    assert len(infos) == 1 and "can't reach" in infos[0].getMessage().lower()


async def test_nothing_connects_after_stop(hass: HomeAssistant) -> None:
    h = Harness(hass)
    await h.channel.async_stop()
    await h.channel._async_connect()  # a late reconnect timer
    assert h.transport.connects == []


async def test_paho_transport_disconnect_before_connect_closes_it_for_good(hass: HomeAssistant) -> None:
    from custom_components.halfhour.channel import PahoTransport

    with patch("paho.mqtt.client.Client") as cls:
        t = PahoTransport(hass, HUB)
        t.disconnect()  # nothing to close yet
        await hass.async_add_executor_job(t.connect, "10.0.0.2", 1883, False, False, HUB, TOKEN, ("s", b"off"), 60)
    cls.assert_not_called()


def test_paho_transport_callbacks_default_to_nothing(hass: HomeAssistant) -> None:
    from custom_components.halfhour.channel import PahoTransport

    t = PahoTransport(hass, HUB)
    assert t.on_connect(0) is None and t.on_message("t", b"") is None and t.on_subscribe([0]) is None


class BlockingClient:
    """A stand-in paho Client whose connect blocks until released, to race it with a disconnect."""

    instances: list[BlockingClient] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[str] = []
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        self.on_subscribe: Any = None
        BlockingClient.instances.append(self)

    def ws_set_options(self, **_kw: Any) -> None: ...
    def tls_set(self) -> None: ...
    def username_pw_set(self, *_a: Any) -> None: ...
    def will_set(self, *_a: Any, **_kw: Any) -> None: ...

    def connect(self, *_a: Any, **_kw: Any) -> None:
        self.calls.append("connect")
        self.entered.set()
        assert self.release.wait(5)

    def loop_start(self) -> None:
        self.calls.append("loop_start")

    def loop_stop(self) -> None:
        self.calls.append("loop_stop")

    def disconnect(self) -> None:
        self.calls.append("disconnect")


async def test_paho_transport_unload_during_connect_leaves_no_client_running(hass: HomeAssistant) -> None:
    from custom_components.halfhour.channel import PahoTransport

    BlockingClient.instances = []
    with patch("paho.mqtt.client.Client", BlockingClient):
        t = PahoTransport(hass, HUB)
        connecting = hass.async_add_executor_job(t.connect, "10.0.0.2", 1883, False, False, HUB, TOKEN, ("s", b"off"), 60)
        for _ in range(500):
            if BlockingClient.instances:
                break
            await asyncio.sleep(0.01)
        [client] = BlockingClient.instances
        assert await hass.async_add_executor_job(client.entered.wait, 5)
        closing = hass.async_add_executor_job(t.disconnect)
        await asyncio.sleep(0.05)  # let the disconnect run (or wait) while connect is in flight
        client.release.set()
        await connecting
        await closing
        assert "loop_start" not in client.calls  # no orphan network thread
        assert "disconnect" in client.calls
        assert t._client is None
        # A late reconnect after the unload builds nothing.
        await hass.async_add_executor_job(t.connect, "10.0.0.2", 1883, False, False, HUB, TOKEN, ("s", b"off"), 60)
    assert len(BlockingClient.instances) == 1
