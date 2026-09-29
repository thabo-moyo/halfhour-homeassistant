"""Devices behind the hub: Halfhour's device list for this home, mirrored into HA.

The gateway owns the list and publishes it, retained, on homes/<hub>/devices
with a revision that rises on every change; the newest revision wins and is
kept across restarts, with the hub it came from: another hub's revisions
start again at 0, so a list held for an old hub is dropped, not compared. Each Halfhour device becomes one HA device under the
Halfhour hub device, and its read mappings join the upload roles as
dev.<device id>.<reading>. Nothing here writes to any device.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.storage import Store

from .const import CONF_HUB_ID, DOMAIN, STORAGE_VERSION
from .slots import Kind

if TYPE_CHECKING:
    from . import HalfhourConfigEntry

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY = 1  # s: unload and HA's stop flush it
CONTROLLED_BY = "controlled_by"  # the per-device "Controlled by Halfhour" sensor's key
# The read mappings uploaded as slots, per kind, with how each is read. A
# binary plugged_in is a read mapping too, but not a slot: it stays out.
READINGS: dict[str, dict[str, Kind]] = {
    "battery": {"soc": "percent", "power": "power"},
    "solar": {"power": "power", "energy": "energy"},
    "ev-charger": {"power": "power", "car_soc": "percent"},
    "load": {"power": "power"},
}
_ID = re.compile(r"^[0-9a-f-]{36}$")


class DeviceListError(ValueError):
    """A device list that doesn't match the contract."""


@dataclass(frozen=True)
class HubDevice:
    id: str
    kind: str
    name: str
    label: str | None
    mapping: dict[str, dict[str, Any]]  # field -> {"entity_id", "invert"}
    facts: dict[str, Any]
    kind_label: str | None = None  # what Halfhour calls the kind ("EV charger")

    def readings(self) -> dict[str, Kind]:
        """Mapped read fields uploaded as slots: field -> kind."""
        return {field: kind for field, kind in READINGS.get(self.kind, {}).items() if field in self.mapping}


@dataclass(frozen=True)
class DeviceList:
    revision: int
    devices: tuple[HubDevice, ...]


def parse_devices(payload: bytes | str) -> DeviceList:
    try:
        data = json.loads(payload)
    except ValueError as err:
        raise DeviceListError("not JSON") from err
    return _parse(data)


def _parse(data: Any) -> DeviceList:
    if not isinstance(data, dict) or data.get("v") != 1:
        raise DeviceListError("not a v1 device list")
    revision = data.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise DeviceListError("bad revision")
    raw = data.get("devices")
    if not isinstance(raw, list):
        raise DeviceListError("devices is not a list")
    devices = tuple(_device(d, i) for i, d in enumerate(raw))
    if len({d.id for d in devices}) != len(devices):
        raise DeviceListError("a device id appears twice")
    return DeviceList(revision, devices)


def _device(d: Any, i: int) -> HubDevice:
    where = f"devices[{i}]"
    if not isinstance(d, dict):
        raise DeviceListError(f"{where} is not an object")
    device_id, kind, name, label = d.get("id"), d.get("kind"), d.get("name"), d.get("label")
    if not isinstance(device_id, str) or not _ID.match(device_id):
        raise DeviceListError(f"{where}.id is not a device id")
    if not isinstance(kind, str) or not kind:
        raise DeviceListError(f"{where}.kind is missing")
    if not isinstance(name, str) or not name:
        raise DeviceListError(f"{where}.name is missing")
    if label is not None and not isinstance(label, str):
        raise DeviceListError(f"{where}.label is not text")
    kind_label = d.get("kind_label")
    if kind_label is not None and not isinstance(kind_label, str):
        raise DeviceListError(f"{where}.kind_label is not text")
    mapping, facts = d.get("mapping"), d.get("facts", {})
    if not isinstance(mapping, dict):
        raise DeviceListError(f"{where}.mapping is not an object")
    if not isinstance(facts, dict):
        raise DeviceListError(f"{where}.facts is not an object")
    fields: dict[str, dict[str, Any]] = {}
    for field, m in mapping.items():
        entity_id = m.get("entity_id") if isinstance(m, dict) else None
        if not isinstance(entity_id, str) or "." not in entity_id:
            raise DeviceListError(f"{where}.mapping.{field} names no entity")
        invert = m.get("invert", False)
        if not isinstance(invert, bool):
            raise DeviceListError(f"{where}.mapping.{field}.invert is not true or false")
        fields[field] = {"entity_id": entity_id, "invert": invert}
    return HubDevice(device_id, kind, name, label, fields, dict(facts), kind_label or None)


def _to_store(hub_id: str, devices: DeviceList) -> dict[str, Any]:
    return {
        "v": 1,
        "hub_id": hub_id,
        "revision": devices.revision,
        "devices": [
            {"id": d.id, "kind": d.kind, "kind_label": d.kind_label, "name": d.name, "label": d.label, "mapping": d.mapping, "facts": d.facts}
            for d in devices.devices
        ],
    }


def devices_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.devices")


def device_identifier(entry_id: str, device_id: str) -> tuple[str, str]:
    return (DOMAIN, f"{entry_id}_{device_id}")


def device_info(entry_id: str, device: HubDevice) -> DeviceInfo:
    return DeviceInfo(
        identifiers={device_identifier(entry_id, device.id)},
        name=device.name,
        manufacturer="Halfhour",  # the device as Halfhour models it; nothing names its hardware
        model=device.kind_label or device.kind,
        via_device=(DOMAIN, entry_id),
    )


def expected_entities(entry_id: str, devices: DeviceList | None) -> dict[str, tuple[HubDevice, str]]:
    """unique_id -> (device, key) for every entity the device list calls for."""
    out: dict[str, tuple[HubDevice, str]] = {}
    for device in devices.devices if devices is not None else ():
        for key in (CONTROLLED_BY, *device.readings()):
            out[f"{entry_id}_{device.id}_{key}"] = (device, key)
    return out


def _issue_prefix(entry_id: str) -> str:
    return f"{entry_id}_device_entity_missing_"


@callback
def async_delete_device_issues(hass: HomeAssistant, entry_id: str, keep: set[str] | None = None) -> None:
    """Delete this entry's missing-entity repair issues, except for the device ids in keep."""
    prefix = _issue_prefix(entry_id)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefix) and issue_id[len(prefix) :] not in (keep or set()):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


class HubDevices:
    """The newest device list for one hub, kept in HA's device and entity registries."""

    def __init__(self, hass: HomeAssistant, entry: HalfhourConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._hub_id = entry.data[CONF_HUB_ID]  # the hub this list belongs to; saves are stamped with this,
        # not entry.data at save time, which reauth/reconfigure may have already moved on
        self.devices: DeviceList | None = None
        self._store = devices_store(hass, entry.entry_id)
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._unsaved = False
        self._started = False

    @property
    def revision(self) -> int | None:
        return self.devices.revision if self.devices is not None else None

    def device(self, device_id: str) -> HubDevice | None:
        return next((d for d in self.devices.devices if d.id == device_id), None) if self.devices is not None else None

    @staticmethod
    def roles_of(devices: DeviceList | None) -> dict[str, dict[str, Any]]:
        roles: dict[str, dict[str, Any]] = {}
        for device in devices.devices if devices is not None else ():
            for field, kind in device.readings().items():
                m = device.mapping[field]
                roles[f"dev.{device.id}.{field}"] = {"entity_id": m["entity_id"], "invert": m["invert"], "kind": kind}
        return roles

    def read_roles(self) -> dict[str, dict[str, Any]]:
        """Upload roles for the devices' mapped readings: "dev.<id>.<reading>" -> {entity_id, invert, kind}."""
        return self.roles_of(self.devices)

    # -- lifecycle -----------------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data is None:
            return
        # A list stored before the hub was recorded is taken as this hub's.
        hub_id = data.get("hub_id", self._hub_id)
        if hub_id != self._hub_id:
            # Reauth or reconfigure moved this home to another hub, whose
            # revisions start again at 0: the old list (and, on start, its HA
            # devices, mirrors and upload roles) goes; the new hub's list follows.
            _LOGGER.info("Dropping the device list held for a previous Halfhour hub")
            await self._store.async_remove()
            return
        try:
            self.devices = _parse(data)
        except DeviceListError as err:
            _LOGGER.warning("Ignoring the stored Halfhour device list: %s", err)

    @callback
    def async_start(self) -> None:
        """Bring the registries in line with the held list and follow the entity registry."""
        self._started = True
        self._unsubs.append(self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed))
        self._apply()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._started = False
        if self._unsaved and self.devices is not None:
            # A new Store replaces this one on reload or removal: write now, not later.
            self._unsaved = False
            await self._store.async_save(_to_store(self._hub_id, self.devices))

    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    # -- changes -------------------------------------------------------------------

    @callback
    def offer(self, payload: bytes) -> None:
        """Keep the list in payload if its revision is newer than the one held."""
        try:
            devices = parse_devices(payload)
        except DeviceListError as err:
            _LOGGER.warning("Ignoring a bad device list from Halfhour: %s", err)
            return
        if self.devices is not None and devices.revision <= self.devices.revision:
            _LOGGER.debug("Ignoring Halfhour device list %s: not newer than %s", devices.revision, self.devices.revision)
            return
        self.devices = devices
        self._unsaved = True
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)
        if self._started:
            self._apply()

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        self._unsaved = False
        assert self.devices is not None  # only scheduled once a list is held
        return _to_store(self._hub_id, self.devices)

    @callback
    def _apply(self) -> None:
        entry_id = self.entry.entry_id
        devices = dr.async_get(self.hass)
        entities = er.async_get(self.hass)
        wanted = {d.id: d for d in self.devices.devices} if self.devices is not None else {}
        expected = expected_entities(entry_id, self.devices)
        prefix = f"{entry_id}_"
        for dev in dr.async_entries_for_config_entry(devices, entry_id):
            ours = [i[1][len(prefix) :] for i in dev.identifiers if i[0] == DOMAIN and i[1].startswith(prefix)]
            if not ours:
                continue  # the hub device itself
            gone = ours[0] not in wanted
            for ent in er.async_entries_for_device(entities, dev.id, include_disabled_entities=True):
                if gone or ent.unique_id not in expected:
                    entities.async_remove(ent.entity_id)
            if gone:
                devices.async_remove_device(dev.id)
        if wanted:
            # The hub device first, so via_device resolves for the ones under it.
            devices.async_get_or_create(
                config_entry_id=entry_id, identifiers={(DOMAIN, entry_id)}, name="Halfhour", manufacturer="Halfhour", entry_type=DeviceEntryType.SERVICE
            )
        for device in wanted.values():
            info = device_info(entry_id, device)
            devices.async_get_or_create(
                config_entry_id=entry_id,
                identifiers=info["identifiers"],
                name=device.name,
                manufacturer=info["manufacturer"],
                model=info["model"],
                via_device=(DOMAIN, entry_id),
            )
        self._check_entities()
        for cb in list(self._listeners):
            cb()

    @callback
    def _registry_changed(self, _event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        self._check_entities()

    @callback
    def _check_entities(self) -> None:
        """A repair issue per device with a mapped entity missing from the entity registry."""
        entry_id = self.entry.entry_id
        devices = self.devices.devices if self.devices is not None else ()
        async_delete_device_issues(self.hass, entry_id, keep={d.id for d in devices})
        registry = er.async_get(self.hass)
        for device in devices:
            issue_id = f"{_issue_prefix(entry_id)}{device.id}"
            missing = sorted({m["entity_id"] for m in device.mapping.values() if registry.async_get(m["entity_id"]) is None})
            if not missing:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="device_entity_missing",
                translation_placeholders={"device": device.name, "entities": ", ".join(missing)},
            )
