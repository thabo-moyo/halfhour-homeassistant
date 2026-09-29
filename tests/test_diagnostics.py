"""Diagnostics show the sync state and never leak the token."""

from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.halfhour.const import CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DOMAIN
from custom_components.halfhour.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_show_cursors_and_redact_the_token(recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage: dict) -> None:
    aioclient_mock.get("https://hh.test/api/v1/ha/config", json={"roles": [], "presets": []})
    aioclient_mock.post("https://hh.test/api/v1/ha/telemetry", status=202, json={"accepted": 0})
    aioclient_mock.put("https://hh.test/api/v1/ha/inventory", status=204)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="hub-1",
        data={CONF_URL: "https://hh.test", CONF_TOKEN: "hh_dev_secret", CONF_HUB_ID: "hub-1"},
        options={CONF_MAPPING: {"house_load_w": {"entity_id": "sensor.load", "invert": False, "kind": "power"}}, CONF_ROLES: []},
    )
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.{entry.entry_id}.cursors"
    cursor = "2026-09-28T10:00:00+00:00"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"house_load_w": {"entity_id": "sensor.load", "cursor": cursor}}}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "hh_dev_secret" not in str(diag)
    assert set(diag["cursors"]) == {"house_load_w"}
    assert diag["cursors"]["house_load_w"]["entity_id"] == "sensor.load"
    assert diag["synced_until"] is not None
    assert diag["devices_synced_until"] is None  # no devices behind this home
    assert "queue" not in diag
