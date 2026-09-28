"""The Halfhour integration: push this home's power readings to Halfhour."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HalfhourClient
from .const import CONF_TOKEN, CONF_URL
from .queue import SampleQueue
from .runtime import HalfhourRuntime

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

type HalfhourConfigEntry = ConfigEntry[HalfhourRuntime]


async def async_setup_entry(hass: HomeAssistant, entry: HalfhourConfigEntry) -> bool:
    client = HalfhourClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    queue = SampleQueue(hass, entry.entry_id)
    await queue.async_load()
    runtime = HalfhourRuntime(hass, entry, client, queue)
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HalfhourConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_stop()
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: HalfhourConfigEntry) -> None:
    """Forget the queue when the home is removed."""
    await SampleQueue(hass, entry.entry_id).async_remove()


async def _async_reload(hass: HomeAssistant, entry: HalfhourConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
