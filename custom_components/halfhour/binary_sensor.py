"""Diagnostic binary sensors: uploads getting through, the live channel, a stale Plan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import HalfhourEntity

if TYPE_CHECKING:
    from . import HalfhourConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: HalfhourConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([Connected(entry, "connected"), Live(entry, "live"), PlanStale(entry, "plan_stale")])


class Connected(HalfhourEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool:
        return self.runtime.sync.connected


class Live(HalfhourEntity, BinarySensorEntity):
    """The MQTT channel is up (off when there is none)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool:
        channel = self.runtime.channel
        return channel is not None and channel.live


class PlanStale(HalfhourEntity, BinarySensorEntity):
    """On when there is no Plan to trust: none, too old, or nothing covers now."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool:
        return self.runtime.plans.stale(dt_util.utcnow())
