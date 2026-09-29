"""Devices behind the hub: Halfhour's device list for this home, mirrored into HA.

The gateway owns the list and publishes it, retained, on homes/<hub>/devices
with a revision that rises on every change; the newest revision wins and is
kept across restarts, with the hub it came from: another hub's revisions
start again at 0, so a list held for an old hub is dropped, not compared.

Each Halfhour device becomes a config subentry of the Halfhour entry (so it
shows as its own item on the integration's page, added with "Add device" and
edited with Reconfigure) holding one HA device under the Halfhour hub device;
its read mappings join the upload roles as dev.<device id>.<reading>. The
subentries follow the list: one is added for a new device, retitled on a
rename and removed when the device goes. A subentry the user deletes in HA
deletes the device in Halfhour; until Halfhour confirms, the device is held
back (no subentry, entities or uploads) and the delete is retried on every
device list and start. Nothing here writes to any device.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.storage import Store

from .api import HalfhourClient, HalfhourError, NotFoundError
from .const import CONF_HUB_ID, DOMAIN, STORAGE_VERSION
from .slots import Kind

if TYPE_CHECKING:
    from . import HalfhourConfigEntry

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY = 1  # s: unload and HA's stop flush it
CONTROLLED_BY = "controlled_by"  # the per-device "Controlled by Halfhour" sensor's key
SUBENTRY_TYPE = "device"  # a device behind the hub, as a config subentry; its unique_id is the device id
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


def _to_store(hub_id: str, devices: DeviceList | None, deleting: set[str], shown: set[str]) -> dict[str, Any]:
    return {
        "v": 1,
        "hub_id": hub_id,
        "revision": devices.revision if devices is not None else None,
        "devices": [
            {"id": d.id, "kind": d.kind, "kind_label": d.kind_label, "name": d.name, "label": d.label, "mapping": d.mapping, "facts": d.facts}
            for d in (devices.devices if devices is not None else ())
        ],
        "deleting": sorted(deleting),
        "shown": sorted(shown),
    }


def _ids(value: Any) -> set[str]:
    return {i for i in value if isinstance(i, str) and _ID.match(i)} if isinstance(value, list) else set()


def device_subentries(entry: ConfigEntry[Any]) -> dict[str, ConfigSubentry]:
    """This entry's device subentries by device id."""
    return {s.unique_id: s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE and s.unique_id is not None}


def subentry_revision(subentry: ConfigSubentry) -> int:
    """The device list revision a subentry was made at: a list older than that may not know its device yet."""
    revision = subentry.data.get("revision")
    return revision if isinstance(revision, int) and not isinstance(revision, bool) else 0


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


def expected_entities(entry_id: str, devices: tuple[HubDevice, ...]) -> dict[str, tuple[HubDevice, str]]:
    """unique_id -> (device, key) for every entity these devices call for."""
    out: dict[str, tuple[HubDevice, str]] = {}
    for device in devices:
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
    """The newest device list for one hub, kept as config subentries and in HA's device and entity registries."""

    def __init__(self, hass: HomeAssistant, entry: HalfhourConfigEntry, client: HalfhourClient) -> None:
        self.hass = hass
        self.entry = entry
        self._client = client  # deletes in Halfhour a device whose subentry was deleted here
        self._hub_id = entry.data[CONF_HUB_ID]  # the hub this list belongs to; saves are stamped with this,
        # not entry.data at save time, which reauth/reconfigure may have already moved on
        self.devices: DeviceList | None = None
        self._store = devices_store(hass, entry.entry_id)
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._unsaved = False
        self._started = False
        self._hub_changed = False  # the stored list was another hub's: its subentries go too
        self._deleting: set[str] = set()  # deleted here, held back until Halfhour's list drops them
        self._shown: set[str] = set()  # the device ids with a subentry, as last seen: one missing was deleted
        self._in_flight: set[str] = set()
        self._syncing = False  # changing subentries: the update listener (run eagerly) must not follow our own changes

    @property
    def revision(self) -> int | None:
        return self.devices.revision if self.devices is not None else None

    def visible(self) -> tuple[HubDevice, ...]:
        """The listed devices less those deleted here and not yet gone from Halfhour's list."""
        return tuple(d for d in self.devices.devices if d.id not in self._deleting) if self.devices is not None else ()

    def device(self, device_id: str) -> HubDevice | None:
        return next((d for d in self.visible() if d.id == device_id), None)

    def subentry_id(self, device_id: str) -> str | None:
        subentry = device_subentries(self.entry).get(device_id)
        return subentry.subentry_id if subentry is not None else None

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
        return self.roles_of(DeviceList(self.revision or 0, self.visible()))

    # -- lifecycle -----------------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data is None:
            return
        # A list stored before the hub was recorded is taken as this hub's.
        hub_id = data.get("hub_id", self._hub_id)
        if hub_id != self._hub_id:
            # Reauth or reconfigure moved this home to another hub, whose
            # revisions start again at 0: the old list (and, on start, its
            # subentries, HA devices, mirrors and upload roles) goes; the new
            # hub's list follows.
            _LOGGER.info("Dropping the device list held for a previous Halfhour hub")
            self._hub_changed = True  # the store is overwritten only once start has removed its subentries
            return
        self._deleting, self._shown = _ids(data.get("deleting")), _ids(data.get("shown"))
        if data.get("revision") is None and not data.get("devices"):
            return  # saved before any list arrived
        try:
            self.devices = _parse(data)
        except DeviceListError as err:
            _LOGGER.warning("Ignoring the stored Halfhour device list: %s", err)

    @callback
    def async_start(self) -> None:
        """Bring the subentries and registries in line with the held list, then follow both."""
        self._started = True
        self._unsubs.append(self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed))
        self._unsubs.append(self.entry.add_update_listener(self._entry_updated))
        # A subentry deleted while this entry wasn't running: delete its device too (below, with any still pending).
        self._deleted(self._shown - set(device_subentries(self.entry)), send=False)
        self._apply()
        if self._hub_changed:
            self._hub_changed = False
            self._save()  # now stamped with the new hub
        self._retry_deletes()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._started = False
        if self._unsaved:
            # A new Store replaces this one on reload or removal: write now, not later.
            self._unsaved = False
            await self._store.async_save(self._data_to_save())

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
        newer = self.devices is None or devices.revision > self.devices.revision
        if newer:
            self._deleting &= {d.id for d in devices.devices}  # gone from Halfhour: the delete is done
        self._retry_deletes()  # the list comes again on every reconnect: Halfhour may be back
        if not newer:
            _LOGGER.debug("Ignoring Halfhour device list %s: not newer than %s", devices.revision, self.revision)
            return
        self.devices = devices
        self._save()
        if self._started:
            self._apply()

    @callback
    def _save(self) -> None:
        self._unsaved = True
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        self._unsaved = False
        return _to_store(self._hub_id, self.devices, self._deleting, self._shown)

    async def _entry_updated(self, _hass: HomeAssistant, _entry: ConfigEntry[Any]) -> None:
        """A subentry added (by the add flow), deleted or renamed (by the user): follow it.

        A subentry both added and deleted while this entry wasn't running was
        never shown, so its device isn't deleted in Halfhour: a rare case
        (the add flow while setup is retrying), left to the user to redo.
        """
        if self._syncing:
            return
        now = set(device_subentries(self.entry))
        if now == self._shown:
            # A rename here would be undone by Halfhour's next list: the name
            # is Halfhour's, changed with Reconfigure, so put it back now.
            names = {d.id: d.name for d in self.visible()}
            if any(names.get(i, s.title) != s.title for i, s in device_subentries(self.entry).items()):
                self._sync_subentries()
            return
        gone = self._shown - now
        self._shown = now
        self._deleted(gone)
        self._save()
        self._apply()

    @callback
    def _deleted(self, device_ids: set[str], send: bool = True) -> None:
        """Hold back devices whose subentry was deleted here, and delete them in Halfhour."""
        if not device_ids:
            return
        self._deleting |= device_ids
        self._save()
        for device_id in device_ids if send else ():
            self._delete_later(device_id)

    @callback
    def _retry_deletes(self) -> None:
        if self._started:
            for device_id in self._deleting:
                self._delete_later(device_id)

    @callback
    def _delete_later(self, device_id: str) -> None:
        if device_id not in self._in_flight:
            self._in_flight.add(device_id)
            self.entry.async_create_background_task(self.hass, self._async_delete(device_id), f"halfhour delete device {device_id}")

    async def _async_delete(self, device_id: str) -> None:
        """Delete one device in Halfhour; it stays held back until Halfhour's list drops it."""
        try:
            await self._client.delete_device(device_id)
        except NotFoundError:
            pass  # already gone there
        except HalfhourError as err:
            _LOGGER.warning("Couldn't remove device %s from Halfhour (%s); will try again", device_id, err)
        finally:
            self._in_flight.discard(device_id)

    @callback
    def _apply(self) -> None:
        self._sync_subentries()
        entry_id = self.entry.entry_id
        devices = dr.async_get(self.hass)
        entities = er.async_get(self.hass)
        wanted = {d.id: d for d in self.visible()}
        expected = expected_entities(entry_id, self.visible())
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
            subentry_id = self.subentry_id(device.id)
            dev = devices.async_get_or_create(
                config_entry_id=entry_id,
                config_subentry_id=subentry_id,
                identifiers=info["identifiers"],
                name=device.name,
                manufacturer=info["manufacturer"],
                model=info["model"],
                via_device=(DOMAIN, entry_id),
            )
            if subentry_id is not None and None in dev.config_entries_subentries.get(entry_id, set()):
                # Made before devices were subentries: it belongs to its subentry alone now.
                devices.async_update_device(dev.id, remove_config_entry_id=entry_id, remove_config_subentry_id=None)
        self._check_entities()
        for cb in list(self._listeners):
            cb()

    @callback
    def _sync_subentries(self) -> None:
        """One subentry per listed device: added, retitled, and removed once the list is new enough to have dropped it.

        A subentry the add flow made is newer than the list until Halfhour's
        next one arrives, so a list older than it doesn't remove it.
        """
        self._syncing = True
        try:
            self._sync_subentries_now()
        finally:
            self._syncing = False
        shown = set(device_subentries(self.entry))
        if shown != self._shown:
            self._shown = shown  # our own changes aren't deletions
            self._save()

    @callback
    def _sync_subentries_now(self) -> None:
        entries = self.hass.config_entries
        wanted = {d.id: d for d in self.visible()}
        held = self.devices
        for device_id, subentry in device_subentries(self.entry).items():
            device = wanted.get(device_id)
            if device is not None:
                if subentry.title != device.name:
                    entries.async_update_subentry(self.entry, subentry, title=device.name)
            elif self._hub_changed or (held is not None and held.revision >= subentry_revision(subentry)):
                entries.async_remove_subentry(self.entry, subentry.subentry_id)
        present = device_subentries(self.entry)
        for device_id, device in wanted.items():
            if device_id not in present:
                data = {"kind": device.kind, "revision": held.revision if held is not None else 0}
                entries.async_add_subentry(
                    self.entry, ConfigSubentry(data=MappingProxyType(data), subentry_type=SUBENTRY_TYPE, title=device.name, unique_id=device_id)
                )

    @callback
    def _registry_changed(self, _event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        self._check_entities()

    @callback
    def _check_entities(self) -> None:
        """A repair issue per device with a mapped entity missing from the entity registry."""
        entry_id = self.entry.entry_id
        devices = self.visible()
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
