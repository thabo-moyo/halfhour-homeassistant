"""The Halfhour integration: send this home's half-hour readings to Halfhour."""

from __future__ import annotations

from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from . import stats
from .api import AuthError, HalfhourClient, HalfhourError
from .const import CONF_ROLES, CONF_TOKEN, CONF_URL, DOMAIN, STORAGE_VERSION
from .sync import HalfhourSync, async_delete_issues

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

type HalfhourConfigEntry = ConfigEntry[HalfhourSync]


async def async_setup_entry(hass: HomeAssistant, entry: HalfhourConfigEntry) -> bool:
    client = HalfhourClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    try:
        config = await client.config()
    except AuthError as err:
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="auth_failed") from err
    except HalfhourError as err:
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="cannot_connect") from err
    if config["roles"] != entry.options.get(CONF_ROLES):
        # The server owns the role list; keep a fresh copy so options need no network.
        hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_ROLES: config["roles"]})
    await _async_remove_0_1_leftovers(hass, entry)
    sync = HalfhourSync(hass, entry, client, partial(stats.fetch, hass))
    await sync.async_load()
    entry.runtime_data = sync
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    sync.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HalfhourConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_stop()
        async_delete_issues(hass, entry.entry_id)
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: HalfhourConfigEntry) -> None:
    """Forget the sync cursors and repair issues when the home is removed."""
    async_delete_issues(hass, entry.entry_id)
    # The same key HalfhourSync stores under; no client is needed to delete it.
    await Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.cursors").async_remove()


async def _async_reload(hass: HomeAssistant, entry: HalfhourConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_remove_0_1_leftovers(hass: HomeAssistant, entry: HalfhourConfigEntry) -> None:
    """0.1.x kept a sample queue and a queued-samples sensor; 0.2 has neither."""
    await Store[Any](hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.queue").async_remove()
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(Platform.SENSOR, DOMAIN, f"{entry.entry_id}_queued_samples"):
        registry.async_remove(entity_id)
