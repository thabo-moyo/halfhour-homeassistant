"""Upload finished half-hour slots from HA's recorder statistics, by cursor.

The recorder is the backlog: each mapped role keeps a cursor (the first slot
not yet known to be final at Halfhour) and every sync sends at most one
WINDOW per role from there, hourly statistics where 5-minute ones have been
purged, 5-minute ones after. Nothing is queued locally, so a restart, an
outage or a remapped sensor just means reading the recorder again.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import AuthError, HalfhourClient, RejectedError, RetryLater
from .const import (
    BACKOFF_MAX,
    BACKOFF_START,
    CONF_HUB_ID,
    CONF_MAPPING,
    DOMAIN,
    DRAIN_DELAY,
    FINAL_AFTER,
    MAX_AGE,
    MAX_BATCH,
    MAX_EMPTY_WINDOWS,
    SEND_GAP,
    SHORT_TERM,
    STORAGE_VERSION,
    SYNC_INTERVAL,
    SYNC_OFFSET,
    WINDOW,
)
from .slots import SLOT, Kind, Period, build_slots, slot_floor

if TYPE_CHECKING:
    from . import HalfhourConfigEntry

_LOGGER = logging.getLogger(__name__)

StatPeriod = Literal["5minute", "hour"]
# fetch(entity_id, kind, start, end, period) -> the recorder's rows with start <= row.start < end
Fetch = Callable[[str, Kind, datetime, datetime, StatPeriod], Awaitable[list[Period]]]
Cursors = dict[str, dict[str, Any]]  # role -> {"entity_id", "invert", "kind", "cursor"}

_HOUR = timedelta(hours=1)
_FIVE = timedelta(minutes=5)


def _hour_floor(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def _slot_start(slot: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(slot["slot"])


def role_kind(m: dict[str, Any]) -> Kind:
    """A mapping entry's kind; 0.1.x entries carry only a unit."""
    kind = m.get("kind") or ("percent" if m.get("unit") == "%" else "power")
    return cast(Kind, kind)


def _issue_prefix(entry_id: str) -> str:
    return f"{entry_id}_no_statistics_"


@callback
def async_delete_issues(hass: HomeAssistant, entry_id: str, keep: set[str] | None = None) -> None:
    """Delete this entry's no-statistics repair issues, except for the roles in keep."""
    prefix = _issue_prefix(entry_id)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefix) and issue_id[len(prefix) :] not in (keep or set()):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


class HalfhourSync:
    """One paired home's cursor-driven uploader."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: HalfhourConfigEntry,
        client: HalfhourClient,
        fetch: Fetch,
        device_roles: Callable[[], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        """device_roles gives the devices' read roles ("dev.<id>.<reading>"), uploaded like the house roles."""
        self.hass = hass
        self._device_roles = device_roles
        self.entry = entry
        self.client = client
        self._fetch = fetch
        # {"hub_id": str, "cursors": Cursors}: cursors say what one hub already has.
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.cursors")
        self._cursors: Cursors = {}
        self.connected = False
        self.last_upload: datetime | None = None
        self._backoff = 0
        self._next_try: datetime | None = None
        self._syncing = False
        self._stopped = False
        self._last_send: datetime | None = None  # when the last request went out, for pacing
        self._auth_failed = False
        self._reset_pending = False  # a resync arrived during a sync: apply it when the sync ends
        self._unreachable = False  # logged "unreachable" and not yet "reachable again"
        self._unsubs: list[CALLBACK_TYPE] = []
        self._later: CALLBACK_TYPE | None = None
        self._listeners: list[Callable[[], None]] = []
        # Device roles the gateway dropped as unknown (the device was deleted
        # there): not sent again until the next device list says otherwise.
        self._dropped: set[str] = set()

    # -- state ---------------------------------------------------------------

    def _mapping(self) -> dict[str, dict[str, Any]]:
        """The house roles and the devices' read roles; a role that goes drops its cursor on the next sync."""
        house = cast(dict[str, dict[str, Any]], self.entry.options.get(CONF_MAPPING, {}))
        if self._device_roles is None:
            return house
        return {**house, **{role: m for role, m in self._device_roles().items() if role not in self._dropped}}

    @callback
    def async_device_list_changed(self) -> None:
        """A new device list: it says which device roles exist, so send them all again."""
        self._dropped.clear()

    @property
    def cursors(self) -> Cursors:
        """A copy of {role: {"entity_id", "invert", "kind", "cursor"}} for diagnostics."""
        return {role: dict(c) for role, c in self._cursors.items()}

    def synced_until(self) -> datetime | None:
        """The earliest cursor over the house roles: all the home's data before it is final.

        A device added later backfills from a year back; that must not drag
        the home's own progress back with it (see devices_synced_until).
        """
        return self._earliest(device=False)

    def devices_synced_until(self) -> datetime | None:
        """The earliest cursor over the devices' read roles: how far their backfill has come."""
        return self._earliest(device=True)

    def _earliest(self, device: bool) -> datetime | None:
        known = [self._cursor(role, m) for role, m in self._mapping().items() if role in self._cursors and role.startswith("dev.") == device]
        return min(known) if known else None

    def _cursor(self, role: str, m: dict[str, Any]) -> datetime:
        stored = self._cursors.get(role)
        # A cursor holds for one entity read one way: a new entity, a flipped
        # invert or another kind makes everything sent so far wrong. Cursors
        # saved before invert/kind were stored (0.2.0) are taken as current.
        if (
            stored is not None
            and stored.get("entity_id") == m["entity_id"]
            and stored.get("invert", bool(m.get("invert", False))) == bool(m.get("invert", False))
            and stored.get("kind", role_kind(m)) == role_kind(m)
        ):
            parsed = dt_util.parse_datetime(stored["cursor"])
            if parsed is not None:
                return parsed
        # New or remapped role: read the recorder again from the start.
        return slot_floor(dt_util.utcnow() - MAX_AGE)

    def _window(self) -> timedelta:
        """History per role per request: WINDOW, shrunk so all roles fit MAX_BATCH slots.

        A window of W holds at most W / SLOT slots per role. Whole hours, and
        at least one, so a window of hourly rows always moves the cursor.
        """
        per_role = SLOT * (MAX_BATCH // max(1, len(self._mapping())))
        return max(_HOUR, min(WINDOW, per_role // _HOUR * _HOUR))

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        if "cursors" not in data:
            # Stored before the hub was recorded: assume it is this hub's.
            data = {"hub_id": self.entry.data[CONF_HUB_ID], "cursors": data}
        # A new hub (reconfigure to another server, or reauth after the hub was
        # deleted) has none of this home's history: backfill it from the start.
        same_hub = data["hub_id"] == self.entry.data[CONF_HUB_ID]
        self._cursors = dict(data["cursors"]) if same_hub else {}

    async def _save(self) -> None:
        await self._store.async_save({"hub_id": self.entry.data[CONF_HUB_ID], "cursors": self._cursors})

    async def async_remove(self) -> None:
        await self._store.async_remove()

    async def async_resync(self) -> None:
        """Forget what the hub has (as a hub change does), save that, and sync from the start.

        A sync already running would write its cursors back over the reset,
        so then the reset waits for that sync to end (see async_sync).
        """
        self._reset_pending = True
        if self._syncing:
            return
        await self._apply_reset()
        await self.async_sync()

    async def _apply_reset(self) -> None:
        self._reset_pending = False
        self._cursors = {}
        await self._save()

    # -- scheduling ----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Sync now, then every SYNC_INTERVAL, SYNC_OFFSET after a 5-minute boundary."""
        self.entry.async_create_background_task(self.hass, self.async_sync(), "halfhour first sync")
        now = dt_util.utcnow()
        boundary = now - timedelta(minutes=now.minute % 5, seconds=now.second, microseconds=now.microsecond)
        first = boundary + SYNC_OFFSET
        if first <= now:
            first += SYNC_INTERVAL

        async def _first_tick(_now: datetime) -> None:
            if self._stopped:
                return
            self._unsubs.append(async_track_time_interval(self.hass, self._on_tick, SYNC_INTERVAL))
            await self.async_sync()

        self._unsubs.append(async_call_later(self.hass, (first - now).total_seconds(), _first_tick))

    async def async_stop(self) -> None:
        # An in-flight sync sees this after its awaits and neither saves nor reschedules.
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_later()

    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    async def _on_tick(self, _now: datetime) -> None:
        if self._later is not None:
            return  # a drain or retry is already due; the tick would only crowd it
        await self.async_sync()

    @callback
    def _schedule(self, seconds: float) -> None:
        self._cancel_later()
        if self._stopped:
            return

        async def _run(_now: datetime) -> None:
            self._later = None
            await self.async_sync()

        self._later = async_call_later(self.hass, seconds, _run)

    @callback
    def _cancel_later(self) -> None:
        if self._later is not None:
            self._later()
            self._later = None

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # -- the sync ------------------------------------------------------------

    async def async_sync(self) -> None:
        """Send one window per mapped role in one request, then move the cursors.

        Windows with nothing to send cost no request, so they are skipped
        through in the same call (up to MAX_EMPTY_WINDOWS) instead of each
        waiting DRAIN_DELAY.
        """
        if self._stopped or self._syncing or self._auth_failed:
            return
        now = dt_util.utcnow()
        if self._next_try is not None and now < self._next_try:
            return
        if self._last_send is not None and (gap := (now - self._last_send).total_seconds()) < SEND_GAP:
            # The gateway allows one request per SEND_GAP: come back then, not into a 429.
            self._schedule(SEND_GAP - gap)
            return
        self._syncing = True
        try:
            mapping = self._mapping()
            self._check_statistics(mapping)
            empty = 0
            while True:
                done, slots = await self._collect(mapping, now)
                if self._stopped:
                    return
                if slots:
                    break
                self._move(done, now)
                empty += 1
                if empty >= MAX_EMPTY_WINDOWS or not self._behind(now):
                    await self._save_and_drain(now)
                    return

            # Stamped at the request itself, not the start of the sync: the
            # recorder reads before it can take seconds.
            self._last_send = dt_util.utcnow()
            dropped: tuple[str, ...] = ()
            try:
                dropped = (await self.client.send(slots)).dropped_roles
            except AuthError:
                _LOGGER.warning("Halfhour refused this home's token; pair again to resume uploads")
                self._auth_failed = True
                self.connected = False
                self.entry.async_start_reauth(self.hass)
                return
            except RejectedError as err:
                # Poison data must not block the cursor: skip the window.
                _LOGGER.warning("Halfhour refused %d slots, skipping them: %s", len(slots), err)
                self._reachable()
            except RetryLater as err:
                self.connected = False
                if not self._unreachable:
                    _LOGGER.info("Halfhour unreachable (%s); will keep trying", err)
                    self._unreachable = True
                self._backoff = min(BACKOFF_MAX, self._backoff * 2 if self._backoff else BACKOFF_START)
                wait = max(self._backoff, err.retry_after or 0)
                self._next_try = now + timedelta(seconds=wait)
                if not self._stopped:
                    if empty:
                        await self._save()  # keep the skipped empty windows
                    self._schedule(wait)
                return
            else:
                self._reachable()
                self.connected = True
                self.last_upload = now
            if self._stopped:
                return
            self._move(done, now)
            self._drop(dropped)
            await self._save_and_drain(now)
        finally:
            self._syncing = False
            if self._reset_pending and not self._stopped:
                await self._apply_reset()
                if self._later is None:  # a pending drain or retry will start from the reset anyway
                    self._schedule(0)
            self._notify()

    async def _collect(
        self, mapping: dict[str, dict[str, Any]], now: datetime
    ) -> tuple[dict[str, tuple[dict[str, Any], datetime, datetime]], list[dict[str, Any]]]:
        """Each role's next window: role -> (mapping entry, old cursor, sent-through), and the slots."""
        done: dict[str, tuple[dict[str, Any], datetime, datetime]] = {}
        slots: list[dict[str, Any]] = []
        window = self._window()
        for role, m in mapping.items():
            cursor = self._cursor(role, m)
            end = min(cursor + window, now)
            role_slots, through = await self._role_slots(role, m["entity_id"], role_kind(m), bool(m.get("invert", False)), cursor, end, now)
            done[role] = (m, cursor, through)
            slots.extend(role_slots)
        return done, slots

    async def _role_slots(
        self, role: str, entity_id: str, kind: Kind, invert: bool, cursor: datetime, end: datetime, now: datetime
    ) -> tuple[list[dict[str, Any]], datetime]:
        """The role's slots in [cursor, end), and how far they reach (a slot boundary)."""
        # Floored to a slot so the handover slot keeps every 5-minute row it has.
        five_start = max(cursor, slot_floor(now - SHORT_TERM))
        five = await self._fetch(entity_id, kind, five_start, end, "5minute") if five_start < end else []
        five.sort(key=lambda p: p.start)
        handover = five[0].start if five else end
        hourly: list[Period] = []
        hour_from = _hour_floor(cursor)  # an hourly row starting before a :30 cursor still holds its :30 slot
        if hour_from < handover:
            # Hourly rows only where they don't overlap the 5-minute ones.
            hourly = [p for p in await self._fetch(entity_id, kind, hour_from, handover, "hour") if p.start + _HOUR <= handover]
            hourly.sort(key=lambda p: p.start)
        out: list[dict[str, Any]] = []
        parts: tuple[tuple[list[Period], timedelta, StatPeriod], ...] = ((hourly, _HOUR, "hour"), (five, _FIVE, "5minute"))
        for periods, step, res in parts:
            if not periods:
                continue
            prev_sum: float | None = None
            if kind == "energy":
                first = periods[0].start
                before = await self._fetch(entity_id, kind, first - step, first, res)
                prev_sum = before[-1].sum if before else None
            out.extend(s for s in build_slots(role, kind, periods, invert, prev_sum) if _slot_start(s) >= cursor)
        # Hourly rows only reach whole hours: a window of hourly rows ending at
        # X:30 holds nothing for X:00, so the next window must start there.
        through = slot_floor(end) if five else max(cursor, _hour_floor(end))
        return out, through

    @callback
    def _move(self, done: dict[str, tuple[dict[str, Any], datetime, datetime]], now: datetime) -> None:
        """Advance each role's cursor past what its window covered, never past the first open slot."""
        # The first slot not yet final: at 12:07 that is 11:30 (it ends at 12:00, final at 12:10).
        first_open = slot_floor(now - SLOT - FINAL_AFTER) + SLOT
        cursors: Cursors = {}
        for role, (m, old, through) in done.items():
            new = max(old, min(through, first_open))
            cursors[role] = {"entity_id": m["entity_id"], "invert": bool(m.get("invert", False)), "kind": role_kind(m), "cursor": new.isoformat()}
        self._cursors = cursors

    @callback
    def _drop(self, roles: tuple[str, ...]) -> None:
        """Stop sending device roles the gateway dropped, and forget their cursors."""
        new = {role for role in roles if role.startswith("dev.") and role not in self._dropped}
        if not new:
            return
        _LOGGER.info("Halfhour no longer knows %s (a device removed there?); not sending it until the next device list", ", ".join(sorted(new)))
        self._dropped |= new
        for role in new:
            self._cursors.pop(role, None)

    def _behind(self, now: datetime) -> bool:
        window = self._window()
        return any(self._cursor(role, m) + window < now for role, m in self._mapping().items())

    async def _save_and_drain(self, now: datetime) -> None:
        await self._save()
        if not self._stopped and self._behind(now):
            self._schedule(DRAIN_DELAY)

    @callback
    def _reachable(self) -> None:
        if self._unreachable:
            _LOGGER.info("Halfhour reachable again")
            self._unreachable = False
        self._backoff = 0
        self._next_try = None

    @callback
    def _check_statistics(self, mapping: dict[str, dict[str, Any]]) -> None:
        """Raise a repair issue for each mapped sensor the recorder keeps no statistics for.

        Only once Home Assistant is running: during startup restored and
        MQTT entities may not exist yet, and every restart would raise one.
        """
        if self.hass.state is not CoreState.running:
            return
        prefix = _issue_prefix(self.entry.entry_id)
        async_delete_issues(self.hass, self.entry.entry_id, keep=set(mapping))
        registry = er.async_get(self.hass)
        for role, m in mapping.items():
            entity_id = m["entity_id"]
            issue_id = f"{prefix}{role}"
            if role.startswith("dev.") and registry.async_get(entity_id) is None:
                # The device_entity_missing repair names this one, with the right advice.
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue
            if self._has_statistics(registry, entity_id):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="no_statistics",
                translation_placeholders={"entity_id": entity_id},
            )

    def _has_statistics(self, registry: er.EntityRegistry, entity_id: str) -> bool:
        """A state_class means long-term statistics: from the registry entry, else the live state."""
        reg = registry.async_get(entity_id)
        if reg is not None:
            return bool((reg.capabilities or {}).get("state_class"))
        state = self.hass.states.get(entity_id)
        return state is not None and "state_class" in state.attributes
