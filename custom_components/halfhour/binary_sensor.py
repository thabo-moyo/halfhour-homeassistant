"""Diagnostic binary sensor: did the last upload get through?"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import HalfhourEntity

if TYPE_CHECKING:
    from . import HalfhourConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: HalfhourConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([Connected(entry, "connected")])


class Connected(HalfhourEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool:
        return self.runtime.connected
