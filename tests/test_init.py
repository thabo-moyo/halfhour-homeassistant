"""Setting up and unloading an entry."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.halfhour.const import CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DOMAIN


async def test_setup_creates_diagnostic_entities_and_unloads(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage):
    aioclient_mock.post("https://hh.test/api/v1/ha/telemetry", status=202, json={"accepted": 0})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sam",
        unique_id="hub-1",
        data={CONF_URL: "https://hh.test", CONF_TOKEN: "t", CONF_HUB_ID: "hub-1"},
        options={CONF_MAPPING: {"house_load_w": {"entity_id": "sensor.load", "invert": False, "unit": "W"}}, CONF_ROLES: []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    states = {s.entity_id for s in hass.states.async_all()}
    assert any(e.startswith("sensor.") and e.endswith("_queued_samples") for e in states), states
    assert any(e.startswith("sensor.") and e.endswith("_last_upload") for e in states), states
    assert any(e.startswith("binary_sensor.") and e.endswith("_connected") for e in states), states

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_entity_ids_survive_a_hub_id_change(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage):
    """Reauth after the hub is re-created re-points CONF_HUB_ID; entity ids must not move."""
    aioclient_mock.post("https://hh.test/api/v1/ha/telemetry", status=202, json={"accepted": 0})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sam",
        unique_id="hub-1",
        data={CONF_URL: "https://hh.test", CONF_TOKEN: "t", CONF_HUB_ID: "hub-1"},
        options={CONF_MAPPING: {"house_load_w": {"entity_id": "sensor.load", "invert": False, "unit": "W"}}, CONF_ROLES: []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    before = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert before

    hass.config_entries.async_update_entry(entry, unique_id="hub-2", data={**entry.data, CONF_HUB_ID: "hub-2"})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    after = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert after == before, (before, after)
