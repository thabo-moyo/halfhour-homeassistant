"""Base entity: one Halfhour device per paired home, updated by the sync."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN

if TYPE_CHECKING:
    from . import HalfhourConfigEntry


class HalfhourEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: HalfhourConfigEntry, key: str) -> None:
        self.runtime = entry.runtime_data
        # Keyed on entry_id, not the hub id: a reauth after the hub is
        # deleted and re-created in Halfhour re-points CONF_HUB_ID on this
        # same entry, and entity/device identity must not move when it does.
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="Halfhour", manufacturer="Halfhour", entry_type=DeviceEntryType.SERVICE)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.runtime.add_listener(self.async_write_ha_state))
