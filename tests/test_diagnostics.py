"""Diagnostics never leak the token."""

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.halfhour.const import CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DOMAIN
from custom_components.halfhour.diagnostics import async_get_config_entry_diagnostics


async def test_token_is_redacted(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage):
    aioclient_mock.post("https://hh.test/api/v1/ha/telemetry", status=202, json={"accepted": 0})
    entry = MockConfigEntry(domain=DOMAIN, unique_id="hub-1", data={CONF_URL: "https://hh.test", CONF_TOKEN: "hh_dev_secret", CONF_HUB_ID: "hub-1"}, options={CONF_MAPPING: {}, CONF_ROLES: []})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "hh_dev_secret" not in str(diag)
    assert diag["queue"]["length"] == 0
