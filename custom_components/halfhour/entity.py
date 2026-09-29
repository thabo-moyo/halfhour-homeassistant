"""Base entities: the Halfhour hub device per paired home, and one device per device behind it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .devices import CONTROLLED_BY, HubDevice, device_info

if TYPE_CHECKING:
    from . import HalfhourConfigEntry


class HalfhourEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC

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


class HalfhourDeviceEntity(Entity):
    """An entity of one device behind the hub, updated when the device list changes."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: HalfhourConfigEntry, device: HubDevice, key: str) -> None:
        self.devices = entry.runtime_data.devices
        self.device_id = device.id
        self.key = key
        self._attr_unique_id = f"{entry.entry_id}_{device.id}_{key}"
        self._attr_translation_key = key if key == CONTROLLED_BY else f"device_{key}"
        self._attr_device_info = device_info(entry.entry_id, device)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.devices.add_listener(self._devices_changed))

    @callback
    def _devices_changed(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self.devices.device(self.device_id) is not None
