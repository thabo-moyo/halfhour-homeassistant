"""What a loaded entry holds: the uploader, the live channel, the current Plan and the devices."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .channel import HalfhourChannel
from .const import CLOCK_SKEW, DOMAIN, STORAGE_VERSION
from .devices import HubDevices
from .inventory import InventoryUploader
from .plan import Plan, PlanError, PlanSlot, covering_slot, from_store, is_stale, newer, to_store
from .sync import HalfhourSync

_LOGGER = logging.getLogger(__name__)

# (entry data, entry options less the server's role list): what a reload must follow.
type ReloadKey = tuple[dict[str, Any], dict[str, Any]]

_NUDGE = timedelta(seconds=1)  # re-evaluate just after a change is due, never just before
SAVE_DELAY = 30  # s: a burst of Plans is one write; unload and HA's stop flush it


def plan_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.plan")


class Plans:
    """The newest Plan seen, persisted, with a tick whenever what it says may change.

    Listeners are told on a new Plan, at every half-hour boundary, when the
    covering slot ends and when the Plan turns stale.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, stale_after_s: int) -> None:
        self.hass = hass
        self.stale_after_s = stale_after_s
        self.plan: Plan | None = None
        self._store = plan_store(hass, entry_id)
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._next: CALLBACK_TYPE | None = None
        self._unsaved = False
        self._skew_logged: set[str] = set()

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data is None:
            return
        try:
            self.plan = from_store(data)
        except PlanError as err:
            _LOGGER.warning("Ignoring the stored Halfhour plan: %s", err)

    @callback
    def async_start(self) -> None:
        self._unsubs.append(async_track_time_change(self.hass, self._tick, minute=[0, 30], second=1))
        self._schedule()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_next()
        if self._unsaved and self.plan is not None:
            # A new Store replaces this one on reload or removal: write now, not later.
            self._unsaved = False
            await self._store.async_save(to_store(self.plan))

    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    # -- what the entities read ------------------------------------------------------

    def slot(self, now: datetime) -> PlanSlot | None:
        return covering_slot(self.plan, now) if self.plan is not None else None

    def stale(self, now: datetime) -> bool:
        return is_stale(self.plan, now, self.stale_after_s)

    # -- changes -------------------------------------------------------------------

    @callback
    def offer(self, plan: Plan) -> None:
        """Keep plan if it is newer than the one held; an older or equal one is dropped."""
        if not newer(self.plan, plan):
            _LOGGER.debug("Ignoring Halfhour plan %s: not newer than %s", plan.id, self.plan.id if self.plan else None)
            return
        if plan.made_at - dt_util.utcnow() > CLOCK_SKEW and plan.id not in self._skew_logged:
            self._skew_logged.add(plan.id)
            _LOGGER.warning(
                "Halfhour plan %s was made at %s, ahead of this Home Assistant's clock (%s); check the clock",
                plan.id,
                plan.made_at.isoformat(),
                dt_util.utcnow().isoformat(),
            )
        self.plan = plan
        self._unsaved = True
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)
        self._changed()

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        self._unsaved = False
        assert self.plan is not None  # only scheduled once a plan is held
        return to_store(self.plan)

    @callback
    def set_stale_after(self, seconds: int) -> None:
        if seconds != self.stale_after_s:
            self.stale_after_s = seconds
            self._changed()

    @callback
    def _changed(self) -> None:
        self._schedule()
        for cb in list(self._listeners):
            cb()

    @callback
    def _tick(self, _now: datetime) -> None:
        self._changed()

    @callback
    def _schedule(self) -> None:
        """One timer at the next moment the covering slot or Stale changes."""
        self._cancel_next()
        if self.plan is None:
            return
        now = dt_util.utcnow()
        moments = [self.plan.made_at + timedelta(seconds=self.stale_after_s)]
        moments += [t for s in self.plan.slots for t in (s.start, s.end)]
        future = [t for t in moments if t > now]
        if not future:
            return

        @callback
        def _due(_now: datetime) -> None:
            self._next = None
            self._changed()

        self._next = async_track_point_in_utc_time(self.hass, _due, min(future) + _NUDGE)

    @callback
    def _cancel_next(self) -> None:
        if self._next is not None:
            self._next()
            self._next = None


@dataclass
class HalfhourRuntime:
    """entry.runtime_data: the uploader, the live channel (None without a broker), the Plan and the devices."""

    sync: HalfhourSync
    channel: HalfhourChannel | None
    plans: Plans
    reload_key: ReloadKey  # the entry data and options that need a reload to change
    broker: tuple[str | None, str | None]  # (url, account) from /ha/config at setup
    devices: HubDevices  # the devices behind this hub, from the retained device list
    inventory: InventoryUploader  # sends the entity inventory the device forms pick from

    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Call cb on any change an entity may show; returns one unsubscribe for all."""
        unsubs = [self.sync.add_listener(cb), self.plans.add_listener(cb), self.devices.add_listener(cb)]
        if self.channel is not None:
            unsubs.append(self.channel.add_listener(cb))

        def _remove() -> None:
            for unsub in unsubs:
                unsub()

        return _remove
