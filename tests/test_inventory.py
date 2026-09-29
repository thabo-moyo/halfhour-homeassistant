"""The entity inventory: registry metadata only, sent on setup and debounced on change."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.halfhour.api import AuthError, RejectedError, RetryLater
from custom_components.halfhour.const import CONF_HUB_ID, CONF_TOKEN, CONF_URL, DOMAIN
from custom_components.halfhour.inventory import (
    INVENTORY_DEBOUNCE,
    INVENTORY_GAP,
    INVENTORY_REFRESH,
    INVENTORY_RETRY,
    MAX_BYTES,
    MAX_ENTITIES,
    InventoryUploader,
    build_inventory,
)

ALLOWED = {"sensor", "binary_sensor", "switch", "input_boolean", "number", "input_number", "select"}


class FakeClient:
    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.sent: list[list[dict[str, Any]]] = []

    async def put_inventory(self, entities: list[dict[str, Any]]) -> None:
        self.sent.append(entities)
        if self.script:
            r = self.script.pop(0)
            if isinstance(r, Exception):
                raise r


def make_entry(hass: HomeAssistant) -> MockConfigEntry:
    e = MockConfigEntry(domain=DOMAIN, unique_id="hub-1", data={CONF_URL: "https://hh.test", CONF_TOKEN: "t", CONF_HUB_ID: "hub-1"})
    e.add_to_hass(hass)
    return e


def populate(hass: HomeAssistant) -> MockConfigEntry:
    """A registry with sensors, a switch, a number, a select, a light and some to leave out."""
    other = MockConfigEntry(domain="acme")
    other.add_to_hass(hass)
    reg = er.async_get(hass)
    areas = ar.async_get(hass)
    garage = areas.async_create("Garage")
    loft = areas.async_create("Loft")
    device = dr.async_get(hass).async_get_or_create(config_entry_id=other.entry_id, identifiers={("acme", "inv1")}, name="Inverter")
    dr.async_get(hass).async_update_device(device.id, area_id=garage.id)
    reg.async_get_or_create(
        "sensor",
        "acme",
        "soc",
        suggested_object_id="inverter_soc",
        device_id=device.id,
        has_entity_name=True,
        original_name="Battery",
        original_device_class="battery",
        unit_of_measurement="%",
        capabilities={"state_class": "measurement"},
        config_entry=other,
    )
    reg.async_get_or_create(
        "sensor",
        "acme",
        "power",
        suggested_object_id="inverter_power",
        original_name="Inverter power",
        original_device_class="power",
        unit_of_measurement="kW",
        capabilities={"state_class": "measurement"},
    )
    reg.async_update_entity_options("sensor.inverter_power", "sensor", {"unit_of_measurement": "W"})
    reg.async_get_or_create("switch", "acme", "sw", suggested_object_id="immersion", original_name="Immersion")
    reg.async_update_entity("switch.immersion", area_id=loft.id, name="Hot water")
    reg.async_get_or_create(
        "number", "acme", "setpoint", suggested_object_id="grid_setpoint", original_name="Grid setpoint", unit_of_measurement="W", capabilities={"min": -5000, "max": 5000, "step": 10}
    )
    reg.async_get_or_create("select", "acme", "mode", suggested_object_id="mode", original_name="Mode", capabilities={"options": ["auto", "off"]})
    reg.async_get_or_create("light", "acme", "lamp", suggested_object_id="lamp", original_name="Lamp")
    reg.async_get_or_create("sensor", "acme", "off", suggested_object_id="disabled", original_name="Off", disabled_by=er.RegistryEntryDisabler.USER)
    reg.async_get_or_create("sensor", DOMAIN, "ours", suggested_object_id="halfhour_last_upload", original_name="Last upload")
    reg.async_get_or_create("binary_sensor", "acme", "plug", suggested_object_id="plugged", original_device_class="plug")
    # Live states carry values and attributes; none of it may leave the home.
    hass.states.async_set("sensor.inverter_soc", "57", {"friendly_name": "Inverter Battery", "secret": "x"})
    hass.states.async_set("switch.immersion", "on", {"friendly_name": "Hot water"})
    return other


async def test_the_inventory_is_registry_metadata_only(hass: HomeAssistant) -> None:
    populate(hass)
    inventory = build_inventory(hass)
    by_id = {e["entity_id"]: e for e in inventory}
    assert set(by_id) == {"sensor.inverter_soc", "sensor.inverter_power", "switch.immersion", "number.grid_setpoint", "select.mode", "binary_sensor.plugged"}
    assert by_id["sensor.inverter_soc"] == {
        "entity_id": "sensor.inverter_soc",
        "name": "Inverter Battery",
        "area": "Garage",
        "domain": "sensor",
        "device_class": "battery",
        "unit": "%",
        "state_class": "measurement",
    }
    assert by_id["sensor.inverter_power"]["unit"] == "W"  # the unit the user chose wins
    assert by_id["sensor.inverter_power"]["name"] == "Inverter power"
    assert by_id["switch.immersion"] == {"entity_id": "switch.immersion", "name": "Hot water", "area": "Loft", "domain": "switch"}
    assert by_id["number.grid_setpoint"] == {
        "entity_id": "number.grid_setpoint",
        "name": "Grid setpoint",
        "domain": "number",
        "unit": "W",
        "min": -5000,
        "max": 5000,
        "step": 10,
    }
    assert by_id["select.mode"]["options"] == ["auto", "off"]
    assert by_id["binary_sensor.plugged"] == {"entity_id": "binary_sensor.plugged", "name": "binary_sensor.plugged", "domain": "binary_sensor", "device_class": "plug"}
    for e in inventory:
        assert e["domain"] in ALLOWED
        assert "state" not in e and "attributes" not in e
    text = json.dumps(inventory)
    assert "57" not in text and "secret" not in text and '"on"' not in text


async def test_the_inventory_is_capped(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    reg = er.async_get(hass)
    for i in range(MAX_ENTITIES + 3):
        reg.async_get_or_create("sensor", "acme", f"s{i}", suggested_object_id=f"s{i:05d}")
    inventory = build_inventory(hass)
    assert len(inventory) == MAX_ENTITIES
    assert inventory[0]["entity_id"] == "sensor.s00000"
    assert "only the first" in caplog.text


async def test_the_inventory_fits_the_byte_limit(hass: HomeAssistant) -> None:
    reg = er.async_get(hass)
    for i in range(1000):
        reg.async_get_or_create("sensor", "acme", f"s{i}", suggested_object_id=f"s{i:04d}", original_name="x" * 2000)
    inventory = build_inventory(hass)
    assert 0 < len(inventory) < 1000
    assert len(json.dumps({"v": 1, "entities": inventory}).encode()) <= MAX_BYTES


# -- the uploader -----------------------------------------------------------------


async def later(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_sent_on_start_then_bursts_coalesce(hass: HomeAssistant, freezer: Any) -> None:
    populate(hass)
    client = FakeClient()
    uploader = InventoryUploader(hass, make_entry(hass), client)  # type: ignore[arg-type]
    uploader.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(client.sent) == 1

    reg = er.async_get(hass)
    for i in range(5):
        reg.async_get_or_create("sensor", "acme", f"new{i}", suggested_object_id=f"new{i}")
        await hass.async_block_till_done()
    dr.async_get(hass).async_update_device(next(iter(dr.async_get(hass).devices)), name_by_user="Big inverter")
    await hass.async_block_till_done()
    assert len(client.sent) == 1  # nothing yet: the burst is debounced
    await later(hass, freezer, INVENTORY_DEBOUNCE + 1)
    assert len(client.sent) == 2
    assert {e["entity_id"] for e in client.sent[1]} >= {f"sensor.new{i}" for i in range(5)}
    assert next(e for e in client.sent[1] if e["entity_id"] == "sensor.inverter_soc")["name"] == "Big inverter Battery"
    await later(hass, freezer, INVENTORY_DEBOUNCE * 3)
    assert len(client.sent) == 2  # quiet registry, no more uploads

    await uploader.async_stop()
    reg.async_get_or_create("sensor", "acme", "after", suggested_object_id="after")
    await later(hass, freezer, INVENTORY_DEBOUNCE + 1)
    assert len(client.sent) == 2  # stopped: registry changes are no longer followed


@pytest.mark.parametrize("error", [AuthError("nope"), RejectedError("too big")])
async def test_a_refused_upload_is_not_fatal(hass: HomeAssistant, freezer: Any, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.halfhour")
    client = FakeClient(error)
    uploader = InventoryUploader(hass, make_entry(hass), client)  # type: ignore[arg-type]
    uploader.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(client.sent) == 1
    assert "inventory" in caplog.text
    await later(hass, freezer, INVENTORY_RETRY + 1)
    assert len(client.sent) == 1  # a refusal is not retried by itself...
    er.async_get(hass).async_get_or_create("sensor", "acme", "x", suggested_object_id="x")
    await later(hass, freezer, INVENTORY_DEBOUNCE + 1)
    assert len(client.sent) == 2  # ...but the next change sends again
    await uploader.async_stop()


async def test_an_unreachable_gateway_is_retried(hass: HomeAssistant, freezer: Any) -> None:
    client = FakeClient(RetryLater(), RetryLater(retry_after=600))
    uploader = InventoryUploader(hass, make_entry(hass), client)  # type: ignore[arg-type]
    uploader.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    await later(hass, freezer, INVENTORY_RETRY + 1)
    assert len(client.sent) == 2
    await later(hass, freezer, INVENTORY_RETRY + 1)
    assert len(client.sent) == 2  # the gateway asked for 600 s
    await later(hass, freezer, 600 - INVENTORY_RETRY)
    assert len(client.sent) == 3
    await uploader.async_stop()


async def test_stop_cancels_a_pending_retry(hass: HomeAssistant, freezer: Any) -> None:
    client = FakeClient(RetryLater())
    uploader = InventoryUploader(hass, make_entry(hass), client)  # type: ignore[arg-type]
    uploader.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    await uploader.async_stop()
    await later(hass, freezer, INVENTORY_RETRY + 1)
    assert len(client.sent) == 1


async def test_resent_every_six_hours_so_a_quiet_home_never_looks_stale(hass: HomeAssistant, freezer: Any) -> None:
    client = FakeClient()
    uploader = InventoryUploader(hass, make_entry(hass), client)  # type: ignore[arg-type]
    uploader.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert INVENTORY_REFRESH == timedelta(hours=6)
    await later(hass, freezer, INVENTORY_REFRESH.total_seconds() - 60)
    assert len(client.sent) == 1
    await later(hass, freezer, 61)
    assert len(client.sent) == 2
    await later(hass, freezer, INVENTORY_REFRESH.total_seconds())
    assert len(client.sent) == 3
    await uploader.async_stop()
    await later(hass, freezer, INVENTORY_REFRESH.total_seconds())
    assert len(client.sent) == 3


async def test_a_reconnect_resends_it_within_the_gateways_limit(hass: HomeAssistant, freezer: Any) -> None:
    client = FakeClient()
    uploader = InventoryUploader(hass, make_entry(hass), client)  # type: ignore[arg-type]
    uploader.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    uploader.async_request()  # the first connect, right after setup's own upload
    await later(hass, freezer, INVENTORY_GAP + 1)
    assert len(client.sent) == 1  # just sent: nothing new to say

    await later(hass, freezer, 600)
    uploader.async_request()  # a reconnect later on
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(client.sent) == 2
    uploader.async_request()
    uploader.async_request()
    await later(hass, freezer, INVENTORY_GAP + 1)
    assert len(client.sent) == 2  # flapping connects don't hammer the gateway
    await uploader.async_stop()


async def test_a_reconnect_after_a_failed_upload_waits_out_the_gap(hass: HomeAssistant, freezer: Any) -> None:
    client = FakeClient(RetryLater(status=503))
    uploader = InventoryUploader(hass, make_entry(hass), client)  # type: ignore[arg-type]
    uploader.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    uploader.async_request()  # replaces the 300 s retry: the channel is back, so is the gateway, likely
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(client.sent) == 1  # but not inside the gateway's 30 s window
    await later(hass, freezer, INVENTORY_GAP + 1)
    assert len(client.sent) == 2
    await uploader.async_stop()


async def test_a_429_just_reschedules(hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture) -> None:
    client = FakeClient(RetryLater(retry_after=17, status=429))
    uploader = InventoryUploader(hass, make_entry(hass), client)  # type: ignore[arg-type]
    with caplog.at_level(logging.INFO, logger="custom_components.halfhour"):
        uploader.async_start()
        await hass.async_block_till_done(wait_background_tasks=True)
        await later(hass, freezer, 16)
        assert len(client.sent) == 1
        await later(hass, freezer, 2)
        assert len(client.sent) == 2
    assert not caplog.records  # routine: nothing above debug
    await uploader.async_stop()


async def test_a_gateway_without_inventories_is_logged_once_and_asked_again_every_six_hours(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeClient(*[RetryLater(status=404, detail="Not Found") for _ in range(3)])
    uploader = InventoryUploader(hass, make_entry(hass), client)  # type: ignore[arg-type]
    with caplog.at_level(logging.INFO, logger="custom_components.halfhour"):
        uploader.async_start()
        await hass.async_block_till_done(wait_background_tasks=True)
        await later(hass, freezer, INVENTORY_RETRY * 10)
        assert len(client.sent) == 1  # not every 300 s
        await later(hass, freezer, INVENTORY_REFRESH.total_seconds())
        assert len(client.sent) == 2
    assert [r.levelname for r in caplog.records] == ["INFO"]
    assert "every 6 hours" in caplog.records[0].getMessage()
    await later(hass, freezer, INVENTORY_REFRESH.total_seconds())
    assert len(client.sent) == 3
    await later(hass, freezer, INVENTORY_REFRESH.total_seconds())
    assert len(client.sent) == 4  # a newer gateway takes it: sent as usual
    await uploader.async_stop()


async def test_a_gateway_without_inventories_backs_off_reconnects_for_six_hours(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A reconnect (async_request) must not PUT again before the 6 h backoff a 404 sets, same as the doc says for the periodic refresh."""
    client = FakeClient(RetryLater(status=404, detail="Not Found"), RetryLater(status=404, detail="Not Found"))
    uploader = InventoryUploader(hass, make_entry(hass), client)  # type: ignore[arg-type]
    with caplog.at_level(logging.INFO, logger="custom_components.halfhour"):
        uploader.async_start()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(client.sent) == 1  # the setup attempt, refused

        await later(hass, freezer, INVENTORY_GAP + 1)  # clear of the gateway's own reconnect gap
        uploader.async_request()  # a reconnect soon after the 404
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(client.sent) == 1  # backed off: no PUT before the 6 h refresh
        uploader.async_request()
        uploader.async_request()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(client.sent) == 1  # flapping reconnects still don't PUT

        await later(hass, freezer, INVENTORY_REFRESH.total_seconds() - (INVENTORY_GAP + 1))
        assert len(client.sent) == 2  # the 6 h refresh tries again, as documented
    assert [r.levelname for r in caplog.records] == ["INFO"]  # logged once, not once per reconnect
    await uploader.async_stop()
