"""Devices behind the hub: the retained device list, HA devices, mirrors, roles and repairs."""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from types import MappingProxyType

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.halfhour.const import CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DOMAIN
from custom_components.halfhour.devices import SAVE_DELAY, DeviceListError, HubDevices, parse_devices

from .fakes import FakeTransport

HUB = "hub-1"
BROKER = "mqtt://192.168.8.200:1883"
TOPIC = f"homes/{HUB}/devices"
ROLES = [{"id": "house_load_w", "label": "House load", "unit": "W", "kind": "power", "required": True, "device_classes": ["power"]}]
LOAD = {"house_load_w": {"entity_id": "sensor.load", "invert": False, "kind": "power"}}

B = "3f2a0000-0000-4000-8000-00000000000b"
E = "3f2a0000-0000-4000-8000-00000000000e"
L = "3f2a0000-0000-4000-8000-00000000000c"
S = "3f2a0000-0000-4000-8000-00000000000d"

BATTERY = {
    "id": B,
    "kind": "battery",
    "kind_label": "Home battery",
    "name": "Home battery",
    "mapping": {"soc": {"entity_id": "sensor.bat_soc"}, "power": {"entity_id": "sensor.bat_power", "invert": True}, "grid_setpoint": {"entity_id": "number.setpoint"}},
    "facts": {"capacity_kwh": 10, "max_charge_w": 3000, "max_discharge_w": 3000},
}
EV = {
    "id": E,
    "kind": "ev-charger",
    "name": "Car charger",
    "mapping": {"plugged_in": {"entity_id": "binary_sensor.plugged"}, "car_soc": {"entity_id": "sensor.car_soc"}, "charge_switch": {"entity_id": "switch.charge"}},
    "facts": {"min_amps": 6, "max_amps": 32, "phases": 1},
}
IMMERSION = {"id": L, "kind": "load", "name": "Immersion", "label": "Immersion", "mapping": {"switch": {"entity_id": "switch.immersion"}}, "facts": {"rated_w": 3000, "run_minutes": 60}}
SOLAR = {"id": S, "kind": "solar", "name": "Roof", "mapping": {"energy": {"entity_id": "sensor.pv_energy"}}, "facts": {}}


def device_list(revision: int, *devices: dict[str, Any]) -> dict[str, Any]:
    return {"v": 1, "revision": revision, "devices": list(devices)}


# -- parse_devices ------------------------------------------------------------------


def test_parse_a_device_list() -> None:
    parsed = parse_devices(json.dumps(device_list(7, BATTERY, IMMERSION)))
    assert parsed.revision == 7
    battery, immersion = parsed.devices
    assert (battery.id, battery.kind, battery.name, battery.label) == (B, "battery", "Home battery", None)
    assert battery.mapping["power"] == {"entity_id": "sensor.bat_power", "invert": True}
    assert battery.facts["capacity_kwh"] == 10
    assert immersion.label == "Immersion"
    assert parse_devices(json.dumps(device_list(0)).encode()).devices == ()


def _with(**changes: Any) -> dict[str, Any]:
    return {**BATTERY, **changes}


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b"[]",
        json.dumps({"v": 2, "revision": 1, "devices": []}),
        json.dumps({"v": 1, "revision": -1, "devices": []}),
        json.dumps({"v": 1, "revision": True, "devices": []}),
        json.dumps({"v": 1, "revision": "3", "devices": []}),
        json.dumps({"v": 1, "revision": 1, "devices": {}}),
        json.dumps(device_list(1, "battery")),  # type: ignore[arg-type]
        json.dumps(device_list(1, _with(id="not-a-uuid"))),
        json.dumps(device_list(1, _with(id=B.upper()))),
        json.dumps(device_list(1, _with(kind=""))),
        json.dumps(device_list(1, _with(name=""))),
        json.dumps(device_list(1, _with(name=3))),
        json.dumps(device_list(1, _with(label=4))),
        json.dumps(device_list(1, _with(kind_label=5))),
        json.dumps(device_list(1, _with(mapping=[]))),
        json.dumps(device_list(1, _with(mapping={"soc": "sensor.x"}))),
        json.dumps(device_list(1, _with(mapping={"soc": {"entity_id": "nodot"}}))),
        json.dumps(device_list(1, _with(mapping={"soc": {"entity_id": "sensor.x", "invert": "yes"}}))),
        json.dumps(device_list(1, _with(facts=[]))),
        json.dumps(device_list(1, BATTERY, BATTERY)),
    ],
)
def test_parse_refuses_a_bad_list(payload: bytes | str) -> None:
    with pytest.raises(DeviceListError):
        parse_devices(payload)


def test_read_roles_cover_only_mapped_read_fields() -> None:
    parsed = parse_devices(json.dumps(device_list(1, BATTERY, EV, IMMERSION, SOLAR, {**IMMERSION, "id": S.replace("d", "f"), "kind": "heat-pump"})))
    assert HubDevices.roles_of(parsed) == {
        f"dev.{B}.soc": {"entity_id": "sensor.bat_soc", "invert": False, "kind": "percent"},
        f"dev.{B}.power": {"entity_id": "sensor.bat_power", "invert": True, "kind": "power"},
        f"dev.{E}.car_soc": {"entity_id": "sensor.car_soc", "invert": False, "kind": "percent"},
        f"dev.{S}.energy": {"entity_id": "sensor.pv_energy", "invert": False, "kind": "energy"},
    }


# -- wired into setup -----------------------------------------------------------------


@pytest.fixture
def transport() -> Iterator[FakeTransport]:
    fake = FakeTransport()
    with patch("custom_components.halfhour.channel.PahoTransport", return_value=fake):
        yield fake


@pytest.fixture
def server(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    aioclient_mock.get("https://hh.test/api/v1/ha/config", json={"roles": ROLES, "presets": [], "mqtt": {"url": BROKER, "account": "acct"}})
    aioclient_mock.post("https://hh.test/api/v1/ha/telemetry", status=202, json={"accepted": 0})
    aioclient_mock.put("https://hh.test/api/v1/ha/inventory", status=204)
    return aioclient_mock


def make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sam",
        unique_id=HUB,
        data={CONF_URL: "https://hh.test", CONF_TOKEN: "t", CONF_HUB_ID: HUB},
        options={CONF_MAPPING: LOAD, CONF_ROLES: ROLES},
    )
    entry.add_to_hass(hass)
    return entry


async def setup(hass: HomeAssistant, transport: FakeTransport) -> MockConfigEntry:
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    transport.on_connect(0)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def deliver(hass: HomeAssistant, transport: FakeTransport, payload: Any) -> None:
    transport.on_message(TOPIC, payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    await hass.async_block_till_done(wait_background_tasks=True)


def halfhour_devices(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, dr.DeviceEntry]:
    reg = dr.async_get(hass)
    return {ident[1]: d for d in dr.async_entries_for_config_entry(reg, entry.entry_id) for ident in d.identifiers if ident[0] == DOMAIN}


def register(hass: HomeAssistant, *entity_ids: str) -> None:
    reg = er.async_get(hass)
    for entity_id in entity_ids:
        domain, object_id = entity_id.split(".")
        reg.async_get_or_create(domain, "acme", object_id, suggested_object_id=object_id)


def subentries(entry: MockConfigEntry) -> dict[str, Any]:
    return {s.unique_id: s for s in entry.subentries.values()}


def issue(hass: HomeAssistant, entry: MockConfigEntry, device_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_device_entity_missing_{device_id}")


async def test_a_device_list_makes_devices_under_the_hub(recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport) -> None:
    entry = await setup(hass, transport)
    assert (TOPIC, 1) in transport.subscribed[0]
    await deliver(hass, transport, device_list(1, BATTERY, IMMERSION))

    devices = halfhour_devices(hass, entry)
    hub = devices[entry.entry_id]
    battery, immersion = devices[f"{entry.entry_id}_{B}"], devices[f"{entry.entry_id}_{L}"]
    assert battery.via_device_id == hub.id and immersion.via_device_id == hub.id
    # Each device is its own subentry of the Halfhour entry, holding its HA device.
    subs = subentries(entry)
    assert {d: s.title for d, s in subs.items()} == {B: "Home battery", L: "Immersion"}
    assert subs[B].subentry_type == "device" and dict(subs[B].data) == {"kind": "battery", "revision": 1}
    assert battery.config_entries_subentries == {entry.entry_id: {subs[B].subentry_id}}
    assert immersion.config_entries_subentries == {entry.entry_id: {subs[L].subentry_id}}
    assert hub.config_entries_subentries == {entry.entry_id: {None}}
    assert (battery.name, battery.model, battery.manufacturer, immersion.name) == ("Home battery", "Home battery", "Halfhour", "Immersion")
    assert (immersion.model, immersion.manufacturer) == ("load", "Halfhour")  # no served label: the kind itself

    # Every device says it is monitored only; nothing writes to hardware.
    for name in ("home_battery", "immersion"):
        s = hass.states.get(f"sensor.{name}_controlled_by_halfhour")
        assert s is not None and s.state == "monitor_only", name
    ent = er.async_get(hass).async_get("sensor.home_battery_controlled_by_halfhour")
    assert ent is not None and ent.entity_category is not None and ent.device_id == battery.id

    ent = er.async_get(hass).async_get("sensor.home_battery_controlled_by_halfhour")
    assert ent is not None and ent.config_subentry_id == subs[B].subentry_id

    # Renamed, then the load deleted.
    await deliver(hass, transport, device_list(2, {**BATTERY, "name": "Garage battery"}))
    devices = halfhour_devices(hass, entry)
    assert devices[f"{entry.entry_id}_{B}"].name == "Garage battery"
    assert {d: s.title for d, s in subentries(entry).items()} == {B: "Garage battery"}
    assert subentries(entry)[B].subentry_id == subs[B].subentry_id  # the same subentry, retitled
    assert not any(c[0] == "DELETE" for c in server.mock_calls)  # Halfhour's own removal isn't sent back
    assert f"{entry.entry_id}_{L}" not in devices
    assert hass.states.get("sensor.immersion_controlled_by_halfhour") is None
    assert er.async_get(hass).async_get("sensor.immersion_controlled_by_halfhour") is None

    # Deleted, then added back.
    await deliver(hass, transport, device_list(3, IMMERSION))
    await deliver(hass, transport, device_list(4, IMMERSION, BATTERY))
    assert hass.states.get("sensor.home_battery_controlled_by_halfhour") is not None
    assert entry.runtime_data.devices.revision == 4


async def test_an_older_or_bad_list_is_ignored(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await setup(hass, transport)
    await deliver(hass, transport, device_list(5, BATTERY))
    await deliver(hass, transport, device_list(4, IMMERSION))
    await deliver(hass, transport, device_list(5, IMMERSION))
    with caplog.at_level(logging.WARNING):
        await deliver(hass, transport, b"{not json")
    assert "Ignoring a bad device list" in caplog.text
    devices = halfhour_devices(hass, entry)
    assert f"{entry.entry_id}_{B}" in devices and f"{entry.entry_id}_{L}" not in devices
    assert entry.runtime_data.devices.revision == 5


async def test_readings_are_mirrored_in_w_and_percent(recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport) -> None:
    hass.states.async_set("sensor.bat_power", "1.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.bat_soc", "57", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.pv_energy", "1234.5", {"unit_of_measurement": "kWh"})
    entry = await setup(hass, transport)
    await deliver(hass, transport, device_list(1, BATTERY, EV, SOLAR))

    power = hass.states.get("sensor.home_battery_power")
    assert power is not None and float(power.state) == -1500.0  # kW to W, then + charging after invert
    assert power.attributes["unit_of_measurement"] == "W" and power.attributes["device_class"] == "power"
    # Display only: sync reads the source entity, so a state_class would only
    # duplicate its statistics (and put a second meter in the Energy dashboard).
    for mirror in ("sensor.home_battery_power", "sensor.home_battery_state_of_charge", "sensor.roof_energy", "sensor.car_charger_car_state_of_charge"):
        assert "state_class" not in hass.states.get(mirror).attributes, mirror  # type: ignore[union-attr]
    assert hass.states.get("sensor.roof_energy").attributes["unit_of_measurement"] == "kWh"  # type: ignore[union-attr]
    assert hass.states.get("sensor.home_battery_state_of_charge").state == "57.0"  # type: ignore[union-attr]
    assert hass.states.get("sensor.roof_energy").state == "1234.5"  # type: ignore[union-attr]
    assert hass.states.get("sensor.car_charger_car_state_of_charge").state == STATE_UNAVAILABLE  # type: ignore[union-attr]
    assert hass.states.get("binary_sensor.car_charger_plugged_in") is None  # not a slot, not mirrored

    hass.states.async_set("sensor.bat_power", "-800", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.car_soc", "80", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.home_battery_power").state) == 800.0  # type: ignore[union-attr]
    assert hass.states.get("sensor.car_charger_car_state_of_charge").state == "80.0"  # type: ignore[union-attr]
    hass.states.async_set("sensor.bat_soc", "unknown")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.home_battery_state_of_charge").state == STATE_UNAVAILABLE  # type: ignore[union-attr]
    for bad in ("nan", "inf", "-inf"):
        hass.states.async_set("sensor.bat_power", bad, {"unit_of_measurement": "W"})
        await hass.async_block_till_done()
        assert hass.states.get("sensor.home_battery_power").state == STATE_UNAVAILABLE, bad  # type: ignore[union-attr]

    # Remapped: the mirror follows the new entity; an unmapped field's mirror goes.
    hass.states.async_set("sensor.other_soc", "33", {"unit_of_measurement": "%"})
    remapped = {**BATTERY, "mapping": {"soc": {"entity_id": "sensor.other_soc"}, "grid_setpoint": {"entity_id": "number.setpoint"}}}
    await deliver(hass, transport, device_list(2, remapped, SOLAR))
    assert hass.states.get("sensor.home_battery_state_of_charge").state == "33.0"  # type: ignore[union-attr]
    assert er.async_get(hass).async_get("sensor.home_battery_power") is None
    assert hass.states.get("sensor.home_battery_power") is None
    hass.states.async_set("sensor.bat_soc", "99", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.home_battery_state_of_charge").state == "33.0"  # type: ignore[union-attr]

    sync = entry.runtime_data.sync
    assert set(sync._mapping()) == {"house_load_w", f"dev.{B}.soc", f"dev.{S}.energy"}


async def test_a_missing_mapped_entity_raises_a_repair_until_it_is_back(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport
) -> None:
    register(hass, "sensor.bat_soc")
    entry = await setup(hass, transport)
    await deliver(hass, transport, device_list(1, BATTERY, IMMERSION))
    found = issue(hass, entry, B)
    assert found is not None
    assert found.translation_key == "device_entity_missing"
    assert found.translation_placeholders == {"device": "Home battery", "entities": "number.setpoint, sensor.bat_power"}
    assert issue(hass, entry, L) is not None

    register(hass, "sensor.bat_power", "number.setpoint")
    await hass.async_block_till_done()
    assert issue(hass, entry, B) is None
    assert issue(hass, entry, L) is not None

    await deliver(hass, transport, device_list(2, BATTERY))  # the load deleted: its issue goes too
    assert issue(hass, entry, L) is None

    er.async_get(hass).async_remove("sensor.bat_power")
    await hass.async_block_till_done()
    assert issue(hass, entry, B) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert issue(hass, entry, B) is None


async def test_the_list_is_restored_on_reload_and_forgotten_on_removal(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport, hass_storage: dict[str, Any]
) -> None:
    entry = await setup(hass, transport)
    await deliver(hass, transport, device_list(3, BATTERY))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.devices.revision == 3
    assert f"dev.{B}.soc" in entry.runtime_data.devices.read_roles()
    assert hass.states.get("sensor.home_battery_controlled_by_halfhour") is not None
    await deliver(hass, transport, device_list(2, IMMERSION))  # older than the restored one
    assert entry.runtime_data.devices.revision == 3

    key = f"{DOMAIN}.{entry.entry_id}.devices"
    assert key in hass_storage
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert key not in hass_storage


async def test_a_bad_stored_list_is_ignored(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport, hass_storage: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    entry = make_entry(hass)
    key = f"{DOMAIN}.{entry.entry_id}.devices"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"v": 1, "revision": "x"}}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.devices.revision is None
    assert entry.runtime_data.devices.read_roles() == {}
    assert "stored Halfhour device list" in caplog.text


async def test_the_inventory_is_sent_on_setup(recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport) -> None:
    register(hass, "sensor.bat_soc")
    await setup(hass, transport)
    puts = [c for c in server.mock_calls if c[0] == "PUT"]
    assert len(puts) == 1
    assert puts[0][2]["v"] == 1
    assert "sensor.bat_soc" in {e["entity_id"] for e in puts[0][2]["entities"]}


async def test_a_new_list_is_saved_after_a_short_delay(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport, hass_storage: dict[str, Any], freezer: Any
) -> None:
    entry = await setup(hass, transport)
    key = f"{DOMAIN}.{entry.entry_id}.devices"
    await deliver(hass, transport, device_list(6, BATTERY))
    freezer.tick(timedelta(seconds=SAVE_DELAY + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["revision"] == 6
    assert hass_storage[key]["data"]["hub_id"] == HUB  # the list is this hub's
    assert hass_storage[key]["data"]["devices"][0]["mapping"]["power"] == {"entity_id": "sensor.bat_power", "invert": True}


def stored_list(hass_storage: dict[str, Any], entry: MockConfigEntry, data: dict[str, Any]) -> None:
    key = f"{DOMAIN}.{entry.entry_id}.devices"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}


async def test_a_list_held_for_another_hub_is_dropped(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport, hass_storage: dict[str, Any]
) -> None:
    """Reauth or reconfigure moved this entry to a new hub, whose revisions start again at 0."""
    entry = make_entry(hass)
    stored_list(hass_storage, entry, {"hub_id": "hub-old", **device_list(9, BATTERY)})
    # What the old hub's list left in HA's device registry.
    dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}, name="Halfhour")
    dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{entry.entry_id}_{B}")}, name="Home battery")
    hass.config_entries.async_add_subentry(entry, ConfigSubentry(data=MappingProxyType({"kind": "battery", "revision": 9}), subentry_type="device", title="Home battery", unique_id=B))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not entry.subentries  # the old hub's device, whatever its revision
    assert not any(c[0] == "DELETE" for c in server.mock_calls)  # nothing to delete at the new hub
    held = entry.runtime_data.devices
    assert held.revision is None
    assert held.read_roles() == {}
    assert not any(r.startswith("dev.") for r in entry.runtime_data.sync._mapping())  # no stale dev. role is uploaded
    assert f"{entry.entry_id}_{B}" not in halfhour_devices(hass, entry)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SAVE_DELAY + 1))
    await hass.async_block_till_done()
    # Overwritten, stamped with the new hub, only once the old subentries are gone.
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}.devices"]["data"]["hub_id"] == HUB
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}.devices"]["data"]["revision"] is None

    transport.on_connect(0)
    await hass.async_block_till_done(wait_background_tasks=True)
    await deliver(hass, transport, device_list(0, IMMERSION))  # the new hub's first list
    assert held.revision == 0
    assert f"{entry.entry_id}_{L}" in halfhour_devices(hass, entry)
    for call in server.mock_calls:
        if call[0] == "POST" and str(call[1]).endswith("/ha/telemetry"):
            assert not any(s["role"].startswith(f"dev.{B}") for s in json.loads(gzip.decompress(call[2]))["slots"])


async def test_a_save_is_stamped_with_the_hub_the_list_was_held_for(
    recorder_mock: Recorder, hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Reauth or reconfigure updates entry.data then reloads: a list received just before that
    must still be flushed stamped with the hub it was held for, not the hub the reload moves to."""
    entry = make_entry(hass)  # entry.data[CONF_HUB_ID] == HUB
    devices = HubDevices(hass, entry, AsyncMock())
    devices.offer(json.dumps(device_list(1, BATTERY)).encode())
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HUB_ID: "hub-2"})
    await devices.async_stop()
    key = f"{DOMAIN}.{entry.entry_id}.devices"
    assert hass_storage[key]["data"]["hub_id"] == HUB  # stamped with the hub it was held for, not hub-2


async def test_a_list_stored_before_hubs_were_recorded_is_this_hubs(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport, hass_storage: dict[str, Any]
) -> None:
    entry = make_entry(hass)
    stored_list(hass_storage, entry, device_list(4, BATTERY))  # 0.4.0 before this fix: no hub_id
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.devices.revision == 4
    assert f"dev.{B}.soc" in entry.runtime_data.devices.read_roles()


async def test_a_new_device_list_resumes_roles_the_gateway_dropped(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport
) -> None:
    entry = await setup(hass, transport)
    await deliver(hass, transport, device_list(1, BATTERY))
    sync = entry.runtime_data.sync
    sync._dropped.add(f"dev.{B}.soc")
    assert f"dev.{B}.soc" not in sync._mapping()
    await deliver(hass, transport, device_list(2, BATTERY, IMMERSION))
    assert f"dev.{B}.soc" in sync._mapping()


async def test_the_inventory_is_resent_when_the_live_channel_reconnects(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport, freezer: Any
) -> None:
    await setup(hass, transport)
    puts = lambda: [c for c in server.mock_calls if c[0] == "PUT"]  # noqa: E731
    assert len(puts()) == 1  # setup's own; the first connect right after adds nothing
    transport.on_disconnect(7)
    await hass.async_block_till_done(wait_background_tasks=True)
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    transport.on_connect(0)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(puts()) == 2


# -- a subentry deleted in Home Assistant -------------------------------------------------


async def remove_subentry(hass: HomeAssistant, entry: MockConfigEntry, device_id: str) -> None:
    """What deleting a device's subentry on the integration's page does."""
    assert hass.config_entries.async_remove_subentry(entry, subentries(entry)[device_id].subentry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def deletes(server: AiohttpClientMocker, device_id: str) -> int:
    return sum(1 for c in server.mock_calls if c[0] == "DELETE" and str(c[1]).endswith(f"/ha/devices/{device_id}"))


async def test_deleting_a_subentry_deletes_the_device_in_halfhour(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport
) -> None:
    server.delete(f"https://hh.test/api/v1/ha/devices/{B}", status=204)
    entry = await setup(hass, transport)
    await deliver(hass, transport, device_list(1, BATTERY, IMMERSION))
    held = entry.runtime_data.devices
    await remove_subentry(hass, entry, B)

    assert deletes(server, B) == 1
    # Held back until Halfhour's list drops it: no subentry, HA device, entities or uploads.
    assert set(subentries(entry)) == {L}
    assert f"{entry.entry_id}_{B}" not in halfhour_devices(hass, entry)
    assert hass.states.get("sensor.home_battery_controlled_by_halfhour") is None
    assert held.device(B) is None and not any(r.startswith(f"dev.{B}") for r in entry.runtime_data.sync._mapping())
    await deliver(hass, transport, device_list(1, BATTERY, IMMERSION))  # the same list again, on a reconnect
    assert set(subentries(entry)) == {L}

    await deliver(hass, transport, device_list(2, IMMERSION))  # Halfhour's list without it
    assert held._deleting == set()
    assert set(subentries(entry)) == {L}
    await deliver(hass, transport, device_list(3, IMMERSION, BATTERY))  # added back in Halfhour: it comes back
    assert set(subentries(entry)) == {L, B}
    assert hass.states.get("sensor.home_battery_controlled_by_halfhour") is not None


async def test_a_failed_delete_is_retried_on_the_next_list_and_after_a_restart(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    server.delete(f"https://hh.test/api/v1/ha/devices/{B}", status=503, json={"detail": "busy"})
    entry = await setup(hass, transport)
    await deliver(hass, transport, device_list(1, BATTERY))
    with caplog.at_level(logging.WARNING):
        await remove_subentry(hass, entry, B)
    assert deletes(server, B) == 1 and "will try again" in caplog.text
    assert not entry.subentries  # still held back while Halfhour has it

    await deliver(hass, transport, device_list(1, BATTERY))  # the retained list, on a reconnect
    assert deletes(server, B) == 2
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert deletes(server, B) == 3
    assert not entry.subentries and entry.runtime_data.devices.device(B) is None

    server.clear_requests()
    server.get("https://hh.test/api/v1/ha/config", json={"roles": ROLES, "presets": [], "mqtt": {"url": BROKER, "account": "acct"}})
    server.delete(f"https://hh.test/api/v1/ha/devices/{B}", status=404, json={"detail": "no such device"})
    await deliver(hass, transport, device_list(1, BATTERY))
    assert deletes(server, B) == 1  # gone there already: nothing more to do but wait for the list
    assert not entry.subentries


async def test_a_subentry_deleted_while_the_entry_was_not_running(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport
) -> None:
    server.delete(f"https://hh.test/api/v1/ha/devices/{B}", status=204)
    entry = await setup(hass, transport)
    await deliver(hass, transport, device_list(1, BATTERY, IMMERSION))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.config_entries.async_remove_subentry(entry, subentries(entry)[B].subentry_id)
    assert deletes(server, B) == 0

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert deletes(server, B) == 1
    assert set(subentries(entry)) == {L}  # not made again from the held list
    assert entry.runtime_data.devices.device(B) is None


async def test_devices_made_before_subentries_move_into_theirs(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport, hass_storage: dict[str, Any]
) -> None:
    """0.4.0 before subentries linked each device to the entry itself."""
    entry = make_entry(hass)
    stored_list(hass_storage, entry, {"hub_id": HUB, **device_list(1, BATTERY)})
    reg = dr.async_get(hass)
    reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}, name="Halfhour")
    reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{entry.entry_id}_{B}")}, name="Home battery")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    sub = subentries(entry)[B]
    assert halfhour_devices(hass, entry)[f"{entry.entry_id}_{B}"].config_entries_subentries == {entry.entry_id: {sub.subentry_id}}


async def test_a_rename_in_home_assistant_is_put_back(
    recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, transport: FakeTransport
) -> None:
    entry = await setup(hass, transport)
    await deliver(hass, transport, device_list(1, BATTERY))
    hass.config_entries.async_update_subentry(entry, subentries(entry)[B], title="Mine")
    await hass.async_block_till_done()
    assert subentries(entry)[B].title == "Home battery"  # renamed with Reconfigure, where Halfhour hears it
    assert not any(c[0] == "DELETE" for c in server.mock_calls)
