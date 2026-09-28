"""Sensors: the upload state (diagnostic) and the live Plan's covering slot."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import HalfhourEntity

if TYPE_CHECKING:
    from . import HalfhourConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: HalfhourConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(
        [
            LastUpload(entry, "last_upload"),
            SyncedUntil(entry, "synced_until"),
            PlanGridPower(entry, "plan_grid_power"),
            PlanBatteryPower(entry, "plan_battery_power"),
            PlanSocTarget(entry, "plan_soc_target"),
            PlanMade(entry, "plan_made"),
        ]
    )


class LastUpload(HalfhourEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self.runtime.sync.last_upload


class SyncedUntil(HalfhourEntity, SensorEntity):
    """Everything before this is final at Halfhour."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self.runtime.sync.synced_until()


class _PlanSlotSensor(HalfhourEntity, SensorEntity):
    """One value of the Plan's slot covering now; unknown without a Plan or covering slot."""

    _attr_entity_category = None  # the Plan's targets are primary, unlike the other entities
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _field: str  # the PlanSlot attribute shown

    @property
    def native_value(self) -> float | None:
        slot = self.runtime.plans.slot(dt_util.utcnow())
        return cast(float | None, getattr(slot, self._field)) if slot is not None else None


class PlanGridPower(_PlanSlotSensor):
    """Planned grid power, + import."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _field = "grid_w"


class PlanBatteryPower(_PlanSlotSensor):
    """Planned battery power, + charging."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _field = "battery_w"


class PlanSocTarget(_PlanSlotSensor):
    """The battery level the Plan aims for in this slot."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _field = "soc"


class PlanMade(HalfhourEntity, SensorEntity):
    """When the Plan in use was made."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        plan = self.runtime.plans.plan
        return plan.made_at if plan is not None else None
