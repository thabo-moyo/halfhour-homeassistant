"""Diagnostic sensors: last upload and how far the sync has reached."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import HalfhourEntity

if TYPE_CHECKING:
    from . import HalfhourConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: HalfhourConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([LastUpload(entry, "last_upload"), SyncedUntil(entry, "synced_until")])


class LastUpload(HalfhourEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self.runtime.last_upload


class SyncedUntil(HalfhourEntity, SensorEntity):
    """Everything before this is final at Halfhour."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self.runtime.synced_until()
