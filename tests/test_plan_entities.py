"""The live channel wired into setup: plan entities, Live, commands, persistence."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.halfhour.const import CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DOMAIN
from custom_components.halfhour.diagnostics import async_get_config_entry_diagnostics

from .fakes import FakeTransport

HUB = "hub-1"
TOKEN = "hh_dev_secret"
BROKER = "mqtt://192.168.8.200:1883"
ROLES = [{"id": "house_load_w", "label": "House load", "unit": "W", "kind": "power", "required": True, "device_classes": ["power"]}]
NEW_ROLES = [*ROLES, {"id": "solar_w", "label": "Solar", "unit": "W", "kind": "power", "required": False, "device_classes": ["power"]}]
LOAD = {"house_load_w": {"entity_id": "sensor.load", "invert": False, "kind": "power"}}
T0 = datetime(2026, 9, 28, 12, 10, tzinfo=UTC)

GRID = "sensor.halfhour_plan_grid_power"
BATTERY = "sensor.halfhour_plan_battery_power"
SOC = "sensor.halfhour_plan_soc_target"
MADE = "sensor.halfhour_plan_made"
STALE = "binary_sensor.halfhour_plan_stale"
LIVE = "binary_sensor.halfhour_live"
PLAN_TOPIC = "accounts/acct/plan"
CMD_TOPIC = f"homes/{HUB}/cmd"
CONFIG_TOPIC = f"homes/{HUB}/config"
STATUS_TOPIC = f"homes/{HUB}/status"


def plan(made_at: str = "2026-09-28T12:00:00+00:00", plan_id: str = "run-1", grid: float = 900) -> dict[str, Any]:
    starts = ["12:00", "12:30", "13:00", "13:30"]
    return {
        "v": 1,
        "id": plan_id,
        "account": "acct",
        "made_at": made_at,
        "slot_minutes": 30,
        "slots": [
            {
                "start": f"2026-09-28T{s}:00+00:00",
                "import_p": 7.0,
                "export_p": 15.0,
                "load_w": 400,
                "pv_w": 0,
                "battery_w": -500 if i == 0 else 1000,
                "grid_w": grid if i == 0 else -200,
                "soc": 60 if i == 0 else 80,
            }
            for i, s in enumerate(starts)
        ],
    }


def config(url: str | None = BROKER, stale_after_s: int = 2700, roles: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"roles": ROLES if roles is None else roles, "presets": [], "mqtt": {"url": url, "stale_after_s": stale_after_s, "account": "acct"}}


def make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sam",
        unique_id=HUB,
        data={CONF_URL: "https://hh.test", CONF_TOKEN: TOKEN, CONF_HUB_ID: HUB},
        options={CONF_MAPPING: LOAD, CONF_ROLES: ROLES},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def transport() -> Iterator[FakeTransport]:
    fake = FakeTransport()
    with patch("custom_components.halfhour.channel.PahoTransport", return_value=fake):
        yield fake


def serve(aioclient_mock: AiohttpClientMocker, body: dict[str, Any]) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get("https://hh.test/api/v1/ha/config", json=body)
    aioclient_mock.post("https://hh.test/api/v1/ha/telemetry", status=202, json={"accepted": 0})
    aioclient_mock.put("https://hh.test/api/v1/ha/inventory", status=204)


async def setup(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, body: dict[str, Any] | None = None) -> MockConfigEntry:
    serve(aioclient_mock, config() if body is None else body)
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def connect(hass: HomeAssistant, transport: FakeTransport) -> None:
    transport.on_connect(0)
    await hass.async_block_till_done(wait_background_tasks=True)


async def deliver(hass: HomeAssistant, transport: FakeTransport, topic: str, payload: dict[str, Any]) -> None:
    transport.on_message(topic, json.dumps(payload).encode())
    await hass.async_block_till_done(wait_background_tasks=True)


def state(hass: HomeAssistant, entity_id: str) -> str:
    s = hass.states.get(entity_id)
    assert s is not None, entity_id
    return s.state


async def tick(hass: HomeAssistant, freezer: Any, to: datetime) -> None:
    freezer.move_to(to)
    async_fire_time_changed(hass, to)
    await hass.async_block_till_done(wait_background_tasks=True)


# -- setup ----------------------------------------------------------------------


async def test_setup_with_a_broker_starts_the_channel(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any
) -> None:
    freezer.move_to(T0)
    await setup(hass, aioclient_mock)
    assert len(transport.connects) == 1
    assert transport.connects[0]["username"] == HUB
    assert transport.connects[0]["host"] == "192.168.8.200"
    assert state(hass, LIVE) == STATE_OFF
    await connect(hass, transport)
    assert state(hass, LIVE) == STATE_ON
    assert (PLAN_TOPIC, 1) in transport.subscribed[-1]
    for entity_id in (GRID, BATTERY, SOC, MADE):
        assert state(hass, entity_id) == STATE_UNKNOWN, entity_id
    assert state(hass, STALE) == STATE_ON  # no plan yet


@pytest.mark.parametrize("body", [config(url=None), {"roles": ROLES, "presets": []}])
async def test_setup_without_a_broker_starts_no_channel(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, body: dict[str, Any]
) -> None:
    entry = await setup(hass, aioclient_mock, body)
    assert transport.connects == []
    assert entry.runtime_data.channel is None
    assert state(hass, LIVE) == STATE_OFF
    for entity_id in (GRID, BATTERY, SOC, MADE):
        assert state(hass, entity_id) == STATE_UNKNOWN, entity_id
    assert state(hass, STALE) == STATE_ON


async def test_an_insecure_broker_raises_the_repair_issue_and_removal_clears_it(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport
) -> None:
    entry = await setup(hass, aioclient_mock, config(url="mqtt://broker.example.com"))
    assert transport.connects == []
    issue_id = f"{entry.entry_id}_insecure_broker"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# -- a refused key ----------------------------------------------------------------


@pytest.mark.parametrize(("status", "reauth"), [(401, True), (503, False), (200, False)])
async def test_a_not_authorised_connect_checks_the_key_over_http(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    transport: FakeTransport,
    status: int,
    reauth: bool,
) -> None:
    """EMQX answers 135 when its auth hook is down too: only Halfhour refusing the key means reauth."""
    entry = await setup(hass, aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get("https://hh.test/api/v1/ha/config", status=status, json=config() if status == 200 else {"detail": "x"})
    with patch("custom_components.halfhour.channel.async_call_later") as later:
        transport.on_connect(135)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 1
    flows = [f for f in hass.config_entries.flow.async_progress() if f["context"].get("source") == "reauth"]
    assert bool(flows) is reauth
    assert later.call_count == (0 if reauth else 1)
    if not reauth:
        assert later.call_args.args[1] >= 60
    assert entry.runtime_data.channel is not None and entry.runtime_data.channel.live is False


# -- plans ----------------------------------------------------------------------


async def test_a_delivered_plan_shows_the_covering_slot(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any
) -> None:
    freezer.move_to(T0)
    await setup(hass, aioclient_mock)
    await connect(hass, transport)
    await deliver(hass, transport, PLAN_TOPIC, plan())
    assert float(state(hass, GRID)) == 900
    assert float(state(hass, BATTERY)) == -500
    assert float(state(hass, SOC)) == 60
    assert datetime.fromisoformat(state(hass, MADE)) == datetime(2026, 9, 28, 12, tzinfo=UTC)
    assert state(hass, STALE) == STATE_OFF
    grid = hass.states.get(GRID)
    assert grid is not None and grid.attributes["unit_of_measurement"] == "W"
    soc = hass.states.get(SOC)
    assert soc is not None and soc.attributes["unit_of_measurement"] == "%"


async def test_a_plan_with_no_covering_slot_is_unknown_and_stale(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any
) -> None:
    freezer.move_to(datetime(2026, 9, 28, 11, 50, tzinfo=UTC))
    await setup(hass, aioclient_mock)
    await connect(hass, transport)
    await deliver(hass, transport, PLAN_TOPIC, plan(made_at="2026-09-28T11:45:00+00:00"))
    assert state(hass, GRID) == STATE_UNKNOWN
    assert state(hass, SOC) == STATE_UNKNOWN
    assert state(hass, MADE) != STATE_UNKNOWN
    assert state(hass, STALE) == STATE_ON
    await tick(hass, freezer, datetime(2026, 9, 28, 12, 0, 1, tzinfo=UTC))
    assert float(state(hass, GRID)) == 900
    assert state(hass, STALE) == STATE_OFF


async def test_a_persisted_plan_is_restored_after_reload(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any, hass_storage: dict[str, Any]
) -> None:
    freezer.move_to(T0)
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    await deliver(hass, transport, PLAN_TOPIC, plan())
    key = f"{DOMAIN}.{entry.entry_id}.plan"

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.plans.plan is not None
    assert float(state(hass, GRID)) == 900  # before any connect

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert key not in hass_storage


async def test_a_corrupt_stored_plan_counts_as_no_plan(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, hass_storage: dict[str, Any]
) -> None:
    serve(aioclient_mock, config())
    entry = make_entry(hass)
    key = f"{DOMAIN}.{entry.entry_id}.plan"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"v": 1, "id": "x"}}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.plans.plan is None
    assert state(hass, GRID) == STATE_UNKNOWN


async def test_an_older_plan_does_not_replace_a_newer_one(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any, hass_storage: dict[str, Any]
) -> None:
    freezer.move_to(T0)
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    await deliver(hass, transport, PLAN_TOPIC, plan(made_at="2026-09-28T12:05:00+00:00", plan_id="new", grid=1234))
    await deliver(hass, transport, PLAN_TOPIC, plan(made_at="2026-09-28T12:00:00+00:00", plan_id="old", grid=1))
    assert float(state(hass, GRID)) == 1234
    assert entry.runtime_data.plans.plan.id == "new"
    await tick(hass, freezer, T0 + timedelta(seconds=31))
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}.plan"]["data"]["id"] == "new"


async def test_a_plan_is_saved_after_a_delay_not_on_every_message(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any, hass_storage: dict[str, Any]
) -> None:
    freezer.move_to(T0)
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    key = f"{DOMAIN}.{entry.entry_id}.plan"
    with patch("homeassistant.helpers.storage.Store._async_write_data") as write:
        await deliver(hass, transport, PLAN_TOPIC, plan(plan_id="a"))
        await deliver(hass, transport, PLAN_TOPIC, plan(made_at="2026-09-28T12:01:00+00:00", plan_id="b"))
        assert key not in hass_storage
        write.assert_not_called()
    await tick(hass, freezer, T0 + timedelta(seconds=31))
    assert hass_storage[key]["data"]["id"] == "b"


async def test_unloading_writes_a_plan_still_waiting_to_be_saved(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any, hass_storage: dict[str, Any]
) -> None:
    freezer.move_to(T0)
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    await deliver(hass, transport, PLAN_TOPIC, plan())
    key = f"{DOMAIN}.{entry.entry_id}.plan"
    assert key not in hass_storage
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass_storage[key]["data"]["id"] == "run-1"


async def test_crossing_a_slot_boundary_updates_the_values(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any
) -> None:
    freezer.move_to(T0)
    await setup(hass, aioclient_mock, config(stale_after_s=86400))
    await connect(hass, transport)
    await deliver(hass, transport, PLAN_TOPIC, plan())
    assert float(state(hass, GRID)) == 900
    await tick(hass, freezer, datetime(2026, 9, 28, 12, 30, 1, tzinfo=UTC))
    assert float(state(hass, GRID)) == -200
    assert float(state(hass, BATTERY)) == 1000
    assert float(state(hass, SOC)) == 80
    await tick(hass, freezer, datetime(2026, 9, 28, 14, 0, 1, tzinfo=UTC))  # past the horizon
    assert state(hass, GRID) == STATE_UNKNOWN
    assert state(hass, STALE) == STATE_ON


async def test_stale_flips_after_stale_after_s(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any
) -> None:
    freezer.move_to(T0)
    await setup(hass, aioclient_mock, config(stale_after_s=2700))  # 45 min: 12:45, not a boundary
    await connect(hass, transport)
    await deliver(hass, transport, PLAN_TOPIC, plan())
    assert state(hass, STALE) == STATE_OFF
    await tick(hass, freezer, datetime(2026, 9, 28, 12, 44, 50, tzinfo=UTC))
    assert state(hass, STALE) == STATE_OFF
    await tick(hass, freezer, datetime(2026, 9, 28, 12, 45, 2, tzinfo=UTC))
    assert state(hass, STALE) == STATE_ON
    assert float(state(hass, GRID)) == -200  # the values stay; Stale says not to trust them


async def test_a_plan_from_the_future_is_logged_once(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    transport: FakeTransport,
    freezer: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(T0)
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    ahead = (T0 + timedelta(minutes=10)).isoformat()
    caplog.set_level(logging.WARNING)
    await deliver(hass, transport, PLAN_TOPIC, plan(made_at=ahead, plan_id="ahead"))
    entry.runtime_data.plans.offer(entry.runtime_data.plans.plan)  # the same plan again (not newer)
    await deliver(hass, transport, PLAN_TOPIC, plan(made_at=ahead, plan_id="ahead"))
    assert sum("clock" in r.getMessage() for r in caplog.records) == 1
    assert state(hass, STALE) == STATE_OFF


# -- commands and config --------------------------------------------------------


async def test_a_resync_command_drops_cursors_and_syncs(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport
) -> None:
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    sync = entry.runtime_data.sync
    with patch.object(sync, "async_resync", wraps=sync.async_resync) as resync:
        await deliver(hass, transport, CMD_TOPIC, {"v": 1, "id": "c1", "name": "resync", "args": {}})
    resync.assert_awaited_once()
    assert transport.sent(f"homes/{HUB}/ack") == [{"v": 1, "id": "c1", "ok": True}]


async def test_an_unknown_command_is_acked_as_failed(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport
) -> None:
    await setup(hass, aioclient_mock)
    await connect(hass, transport)
    await deliver(hass, transport, CMD_TOPIC, {"v": 1, "id": "c9", "name": "explode", "args": {}})
    (ack,) = transport.sent(f"homes/{HUB}/ack")
    assert ack["ok"] is False
    assert "explode" in ack["error"]


async def test_a_reload_config_command_refetches_the_roles(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport
) -> None:
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    runtime = entry.runtime_data
    serve(aioclient_mock, config(roles=NEW_ROLES, stale_after_s=600))
    await deliver(hass, transport, CMD_TOPIC, {"v": 1, "id": "c2", "name": "reload_config", "args": {}})
    assert entry.options[CONF_ROLES] == NEW_ROLES
    assert entry.options[CONF_MAPPING] == LOAD
    assert entry.runtime_data is runtime  # a role refresh is not a reload
    assert runtime.plans.stale_after_s == 600
    assert transport.sent(f"homes/{HUB}/ack") == [{"v": 1, "id": "c2", "ok": True}]


async def test_a_config_message_updates_roles_without_a_fetch(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport
) -> None:
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    runtime = entry.runtime_data
    calls = aioclient_mock.call_count
    await deliver(hass, transport, CONFIG_TOPIC, {"v": 1, "roles": NEW_ROLES, "presets": [], "stale_after_s": 1200})
    assert entry.options[CONF_ROLES] == NEW_ROLES
    assert runtime.plans.stale_after_s == 1200
    assert entry.runtime_data is runtime
    assert not any(method == "GET" for method, *_ in aioclient_mock.mock_calls[calls:])


async def test_an_invalid_config_message_falls_back_to_a_fetch(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport
) -> None:
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    serve(aioclient_mock, config(roles=NEW_ROLES))
    await deliver(hass, transport, CONFIG_TOPIC, {"v": 1, "roles": "nope"})
    assert entry.options[CONF_ROLES] == NEW_ROLES
    assert aioclient_mock.call_count >= 1


async def test_a_new_broker_address_reloads_the_entry(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport
) -> None:
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    runtime = entry.runtime_data
    serve(aioclient_mock, config(url="mqtt://192.168.8.201:1883"))
    await deliver(hass, transport, CMD_TOPIC, {"v": 1, "id": "c3", "name": "reload_config", "args": {}})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not runtime
    assert transport.connects[-1]["host"] == "192.168.8.201"


async def test_a_mapping_change_still_reloads(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport
) -> None:
    entry = await setup(hass, aioclient_mock)
    runtime = entry.runtime_data
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_MAPPING: {}})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data is not runtime


# -- unload and diagnostics -------------------------------------------------------


async def test_unload_stops_the_channel(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport
) -> None:
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert transport.sent(STATUS_TOPIC)[-1]["state"] == "offline"
    assert transport.disconnects >= 1


async def test_diagnostics_show_the_channel_and_plan_without_the_token(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any
) -> None:
    freezer.move_to(T0)
    entry = await setup(hass, aioclient_mock)
    await connect(hass, transport)
    await deliver(hass, transport, PLAN_TOPIC, plan())
    await deliver(hass, transport, CMD_TOPIC, {"v": 1, "id": "c1", "name": "resync", "args": {}})
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert TOKEN not in json.dumps(diag, default=str)
    assert diag["channel"]["live"] is True
    assert diag["channel"]["account"] == "acct"
    assert diag["channel"]["command_ids"] == ["c1"]
    assert diag["plan"]["id"] == "run-1"
    assert diag["plan"]["age_s"] == 600
    assert diag["plan"]["stale"] is False
    assert diag["plan"]["stale_after_s"] == 2700


async def test_diagnostics_without_a_channel_or_plan(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport
) -> None:
    entry = await setup(hass, aioclient_mock, config(url=None))
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["channel"] is None
    assert diag["plan"] is None


async def test_a_restored_plan_long_past_is_stale_with_nothing_scheduled(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any, hass_storage: dict[str, Any]
) -> None:
    freezer.move_to(datetime(2026, 9, 30, 12, 10, tzinfo=UTC))
    serve(aioclient_mock, config())
    entry = make_entry(hass)
    key = f"{DOMAIN}.{entry.entry_id}.plan"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": plan()}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.plans.plan is not None
    assert state(hass, MADE) != STATE_UNKNOWN
    assert state(hass, GRID) == STATE_UNKNOWN
    assert state(hass, STALE) == STATE_ON


# -- picking up a broker later ----------------------------------------------------


@pytest.mark.parametrize("first_url", [None, "mqtt://broker.example.com"])
async def test_without_a_usable_broker_the_config_is_reread_until_one_appears(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any, first_url: str | None
) -> None:
    freezer.move_to(T0)
    entry = await setup(hass, aioclient_mock, config(url=first_url))
    runtime = entry.runtime_data
    assert transport.connects == []

    await tick(hass, freezer, T0 + timedelta(minutes=15, seconds=1))  # still none: no reload
    assert entry.runtime_data is runtime
    assert any(method == "GET" for method, *_ in aioclient_mock.mock_calls)

    serve(aioclient_mock, config(url="mqtts://broker.example.com"))
    await tick(hass, freezer, T0 + timedelta(minutes=30, seconds=2))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not runtime
    assert entry.runtime_data.channel is not None
    assert transport.connects[-1]["tls"] is True
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_insecure_broker") is None

    # With a channel running, nothing re-reads the config on a timer.
    serve(aioclient_mock, config(url=None))
    await tick(hass, freezer, T0 + timedelta(minutes=60, seconds=3))
    assert not aioclient_mock.mock_calls or all(method != "GET" for method, *_ in aioclient_mock.mock_calls)


async def test_a_failed_reread_is_retried_later(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any
) -> None:
    freezer.move_to(T0)
    entry = await setup(hass, aioclient_mock, config(url=None))
    runtime = entry.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get("https://hh.test/api/v1/ha/config", status=503)
    aioclient_mock.post("https://hh.test/api/v1/ha/telemetry", status=202, json={"accepted": 0})
    aioclient_mock.put("https://hh.test/api/v1/ha/inventory", status=204)
    await tick(hass, freezer, T0 + timedelta(minutes=15, seconds=1))
    assert entry.runtime_data is runtime
    assert entry.state is ConfigEntryState.LOADED


async def test_the_reread_timer_stops_on_unload(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any
) -> None:
    freezer.move_to(T0)
    entry = await setup(hass, aioclient_mock, config(url=None))
    assert await hass.config_entries.async_unload(entry.entry_id)
    serve(aioclient_mock, config())
    await tick(hass, freezer, T0 + timedelta(minutes=15, seconds=1))
    assert not any(method == "GET" for method, *_ in aioclient_mock.mock_calls)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_plan_targets_are_primary_measurements(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport
) -> None:
    from homeassistant.helpers import entity_registry as er

    await setup(hass, aioclient_mock)
    registry = er.async_get(hass)
    for entity_id in (GRID, BATTERY, SOC):
        entity = registry.async_get(entity_id)
        assert entity is not None and entity.entity_category is None, entity_id
        s = hass.states.get(entity_id)
        assert s is not None and s.attributes["state_class"] == "measurement", entity_id
    soc = hass.states.get(SOC)
    assert soc is not None and "device_class" not in soc.attributes
    for entity_id in (MADE, STALE, LIVE):
        entity = registry.async_get(entity_id)
        assert entity is not None and entity.entity_category == "diagnostic", entity_id
