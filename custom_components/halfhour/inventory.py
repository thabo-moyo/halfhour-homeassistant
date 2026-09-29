"""The entity inventory: what the home's entities are, never what they read.

Halfhour's device forms offer this home's entities as pickers. The inventory
is built from the entity, device and area registries only: names, areas,
domains, device classes, units and the ranges a number or select allows.
No state and no attribute ever leaves the home through it. It is sent on
setup, on every (re)connect of the live channel, every 6 hours and,
debounced, whenever a registry changes; the gateway keeps the latest one per
hub (replace, not merge), so its age says whether this home is still there.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import AuthError, HalfhourClient, HalfhourError, RetryLater
from .const import DOMAIN

if TYPE_CHECKING:
    from . import HalfhourConfigEntry

_LOGGER = logging.getLogger(__name__)

DOMAINS = frozenset({"sensor", "binary_sensor", "switch", "input_boolean", "number", "input_number", "select"})
MAX_ENTITIES = 5000  # the gateway's limit
MAX_BYTES = 1024 * 1024  # the gateway's body limit
INVENTORY_DEBOUNCE = 60  # s: a burst of registry changes is one upload
INVENTORY_RETRY = 300  # s: after an unreachable gateway
INVENTORY_GAP = 31  # s: the gateway takes one upload per hub per 30 s
INVENTORY_REFRESH = timedelta(hours=6)  # resent this often, so a quiet home's list never looks stale
_RANGE_KEYS = ("min", "max", "step")


def _name(entry: er.RegistryEntry, device: dr.DeviceEntry | None) -> str:
    """What the frontend calls the entity, from the registries alone."""
    if entry.name:
        return entry.name
    device_name = (device.name_by_user or device.name) if device is not None else None
    if entry.has_entity_name and device_name:
        return f"{device_name} {entry.original_name}" if entry.original_name else device_name
    return entry.original_name or entry.entity_id


def _describe(entry: er.RegistryEntry, devices: dr.DeviceRegistry, areas: ar.AreaRegistry) -> dict[str, Any]:
    device = devices.async_get(entry.device_id) if entry.device_id else None
    area_id = entry.area_id or (device.area_id if device is not None else None)
    area = areas.async_get_area(area_id) if area_id else None
    caps = entry.capabilities or {}
    sensor_options: Mapping[str, Any] = entry.options.get("sensor") or {}
    unit = sensor_options.get("unit_of_measurement") or entry.unit_of_measurement
    out: dict[str, Any] = {"entity_id": entry.entity_id, "name": _name(entry, device), "domain": entry.domain}
    optional: dict[str, Any] = {
        "area": area.name if area is not None else None,
        "device_class": entry.device_class or entry.original_device_class,
        "unit": unit,
        "state_class": caps.get("state_class"),
        "options": caps.get("options") if isinstance(caps.get("options"), list) else None,
        **{k: caps.get(k) for k in _RANGE_KEYS if isinstance(caps.get(k), int | float)},
    }
    out.update({k: v for k, v in optional.items() if v is not None})
    return out


@callback
def build_inventory(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every enabled entity of the allowed domains, metadata only, in entity id order."""
    entities, devices, areas = er.async_get(hass), dr.async_get(hass), ar.async_get(hass)
    picked = sorted(
        (e for e in entities.entities.values() if e.domain in DOMAINS and e.disabled_by is None and e.platform != DOMAIN),
        key=lambda e: e.entity_id,
    )
    if len(picked) > MAX_ENTITIES:
        _LOGGER.warning("This home has %d entities Halfhour could use; only the first %d are offered", len(picked), MAX_ENTITIES)
        picked = picked[:MAX_ENTITIES]
    out: list[dict[str, Any]] = []
    size = len(json.dumps({"v": 1, "entities": []}))
    for entry in picked:
        item = _describe(entry, devices, areas)
        size += len(json.dumps(item).encode()) + 2  # ", " between items
        if size > MAX_BYTES:
            _LOGGER.warning("This home's entity inventory is over 1 MiB; only the first %d entities are offered", len(out))
            break
        out.append(item)
    return out


class InventoryUploader:
    """Sends the inventory on start, on request (a reconnect), every 6 h and, debounced, after registry changes."""

    def __init__(self, hass: HomeAssistant, entry: HalfhourConfigEntry, client: HalfhourClient) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self._unsubs: list[CALLBACK_TYPE] = []
        self._later: CALLBACK_TYPE | None = None
        self._stopped = False
        self._last_try: datetime | None = None
        self._last_ok: datetime | None = None
        self._too_old_logged = False  # a gateway without inventories is logged once
        self._not_before: datetime | None = None  # set on a 404, so nothing (including a reconnect) resends before the 6 h refresh

    @callback
    def async_start(self) -> None:
        for event in (er.EVENT_ENTITY_REGISTRY_UPDATED, dr.EVENT_DEVICE_REGISTRY_UPDATED, ar.EVENT_AREA_REGISTRY_UPDATED):
            self._unsubs.append(self.hass.bus.async_listen(event, self._changed))
        self._unsubs.append(async_track_time_interval(self.hass, self._refresh, INVENTORY_REFRESH))
        self._send_now()

    async def async_stop(self) -> None:
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel()

    @callback
    def async_request(self) -> None:
        """Send it again soon (the live channel reconnected), inside the gateway's limit.

        Just sent: nothing new to say. Just tried and failed: wait out the gap.
        """
        now = dt_util.utcnow()
        if self._last_ok is not None and (now - self._last_ok).total_seconds() < INVENTORY_GAP:
            return
        if self._last_try is not None and (gap := (now - self._last_try).total_seconds()) < INVENTORY_GAP:
            self._schedule(INVENTORY_GAP - gap)
            return
        self._send_now()

    @callback
    def _refresh(self, _now: datetime) -> None:
        if self._later is None:  # a pending upload sends the same list anyway
            self._send_now()

    @callback
    def _changed(self, _event: Event[Any]) -> None:
        if self._later is None:  # one upload per burst: later changes ride on the pending one
            self._schedule(INVENTORY_DEBOUNCE)

    @callback
    def _send_now(self) -> None:
        self._cancel()
        if not self._stopped:
            self.entry.async_create_background_task(self.hass, self._send(), "halfhour inventory upload")

    @callback
    def _schedule(self, seconds: float) -> None:
        self._cancel()
        if self._stopped:
            return

        async def _run(_now: datetime) -> None:
            self._later = None
            await self._send()

        self._later = async_call_later(self.hass, seconds, _run)

    @callback
    def _cancel(self) -> None:
        if self._later is not None:
            self._later()
            self._later = None

    async def _send(self) -> None:
        if self._stopped:
            return
        now = dt_util.utcnow()
        if self._not_before is not None and now < self._not_before:
            return  # backed off after a 404; not yet due, so not even a reconnect resends
        self._last_try = now
        try:
            await self.client.put_inventory(build_inventory(self.hass))
        except RetryLater as err:
            if err.status == 404:
                # A gateway from before devices behind a hub: back off 6 h, same as the periodic
                # refresh, so reconnects don't hammer it meanwhile; it's asked again then.
                self._not_before = now + INVENTORY_REFRESH
                if not self._too_old_logged:
                    self._too_old_logged = True
                    _LOGGER.info(
                        "This Halfhour server doesn't take entity inventories yet; asking again every %d hours", INVENTORY_REFRESH.total_seconds() // 3600
                    )
                return
            if err.status == 429:
                _LOGGER.debug("Halfhour asked for the entity inventory later (%s)", err)
                self._schedule(err.retry_after or INVENTORY_GAP)
                return
            _LOGGER.debug("Halfhour unreachable for the entity inventory (%s); trying again later", err)
            self._schedule(max(INVENTORY_RETRY, err.retry_after or 0))
        except AuthError:
            # The uploader starts reauth for a refused token; the next change resends.
            _LOGGER.debug("Halfhour refused this home's token for the entity inventory")
        except HalfhourError as err:
            _LOGGER.warning("Halfhour refused this home's entity inventory: %s", err)
        else:
            self._last_ok = self._last_try
