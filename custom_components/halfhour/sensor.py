"""Sensors: the upload state (diagnostic), the live Plan's covering slot and the devices behind the hub."""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING, cast

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .devices import CONTROLLED_BY, READINGS, HubDevice, expected_entities
from .entity import HalfhourDeviceEntity, HalfhourEntity
from .slots import Kind

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
    devices = entry.runtime_data.devices
    known: set[str] = set()

    @callback
    def _add_devices() -> None:
        """Add the entities a new device list calls for; the list drops removed ones itself."""
        expected = expected_entities(entry.entry_id, devices.devices)
        new: list[SensorEntity] = []
        for unique_id, (device, key) in expected.items():
            if unique_id not in known:
                new.append(ControlledBy(entry, device, key) if key == CONTROLLED_BY else DeviceReading(entry, device, key))
        known.clear()  # a removed device's ids are forgotten, so it can come back
        known.update(expected)
        if new:
            async_add_entities(new)

    _add_devices()
    entry.async_on_unload(devices.add_listener(_add_devices))


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


class ControlledBy(HalfhourDeviceEntity, SensorEntity):
    """Whether Halfhour controls this device: always monitor only, for now."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["monitor_only"]
    _attr_native_value = "monitor_only"


# No state_class: a mirror is display only (sync reads the source entity), and
# one would duplicate the source's statistics and put a second meter in the
# Energy dashboard.
_READING: dict[Kind, tuple[SensorDeviceClass, str]] = {
    "power": (SensorDeviceClass.POWER, UnitOfPower.WATT),
    "energy": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
    "percent": (SensorDeviceClass.BATTERY, PERCENTAGE),
}


class DeviceReading(HalfhourDeviceEntity, SensorEntity):
    """A mapped reading as Halfhour receives it: W (+ charging after invert), kWh or %. Display only."""

    def __init__(self, entry: HalfhourConfigEntry, device: HubDevice, key: str) -> None:
        super().__init__(entry, device, key)
        self._kind: Kind = READINGS[device.kind][key]
        self._attr_device_class, self._attr_native_unit_of_measurement = _READING[self._kind]
        self._source: str | None = None
        self._unfollow: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._follow()
        self.async_on_remove(self._stop_following)

    @callback
    def _devices_changed(self) -> None:
        self._follow()
        super()._devices_changed()

    @callback
    def _follow(self) -> None:
        """Track the mapped entity's state, moving when the device is remapped."""
        device = self.devices.device(self.device_id)
        m = device.mapping.get(self.key) if device is not None else None
        source = m["entity_id"] if m is not None else None
        if source == self._source:
            return
        self._stop_following()
        self._source = source
        if source is not None:
            self._unfollow = async_track_state_change_event(self.hass, [source], self._source_changed)

    @callback
    def _stop_following(self) -> None:
        if self._unfollow is not None:
            self._unfollow()
            self._unfollow = None

    @callback
    def _source_changed(self, _event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    def _read(self) -> float | None:
        device = self.devices.device(self.device_id)
        m = device.mapping.get(self.key) if device is not None else None
        state = self.hass.states.get(m["entity_id"]) if m is not None else None
        if state is None:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        unit = state.attributes.get("unit_of_measurement")
        if self._kind == "power":
            if unit in PowerConverter.VALID_UNITS:
                value = PowerConverter.convert(value, unit, UnitOfPower.WATT)
            return -value if m is not None and m["invert"] else value
        if self._kind == "energy" and unit in EnergyConverter.VALID_UNITS:
            value = EnergyConverter.convert(value, unit, UnitOfEnergy.KILO_WATT_HOUR)
        return value

    @property
    def available(self) -> bool:
        return self._read() is not None

    @property
    def native_value(self) -> float | None:
        return self._read()
