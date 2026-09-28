"""The sync engine: cursors, windows, handover, and every gateway answer."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.halfhour.api import AuthError, RejectedError, RetryLater
from custom_components.halfhour.const import CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DOMAIN, MAX_BATCH, SEND_GAP
from custom_components.halfhour.slots import Period, slot_floor
from custom_components.halfhour.sync import HalfhourSync

NOW = datetime(2026, 9, 28, 12, 7, tzinfo=UTC)


class FakeClient:
    def __init__(self, *script):
        self.script = list(script)
        self.sent: list[list[dict]] = []

    async def send(self, slots):
        self.sent.append(list(slots))
        r = self.script.pop(0) if self.script else len(slots)
        if isinstance(r, Exception):
            raise r
        return r


class FakeStats:
    """hourly/five: entity_id -> list[Period]; answers like the recorder."""

    def __init__(self, hourly=None, five=None):
        self.hourly, self.five, self.asked = hourly or {}, five or {}, []

    async def __call__(self, entity_id, kind, start, end, period):
        self.asked.append((entity_id, period, start, end))
        rows = (self.five if period == "5minute" else self.hourly).get(entity_id, [])
        return [p for p in rows if start <= p.start < end]


def load_entry(hass, mapping) -> MockConfigEntry:
    e = MockConfigEntry(domain=DOMAIN, unique_id="hub-1", data={CONF_URL: "https://hh.test", CONF_TOKEN: "t", CONF_HUB_ID: "hub-1"}, options={CONF_MAPPING: mapping, CONF_ROLES: []})
    e.add_to_hass(hass)
    return e


LOAD = {"house_load_w": {"entity_id": "sensor.load", "invert": False, "kind": "power"}}


def five_min(start, n, mean=400.0):
    return [Period(start + timedelta(minutes=5 * i), 300, mean, None) for i in range(n)]


@pytest.fixture
def now(freezer):
    freezer.move_to(NOW)
    return NOW


async def test_live_sync_sends_complete_and_filling_slots(hass: HomeAssistant, now):
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement", "unit_of_measurement": "W"})
    entry = load_entry(hass, LOAD)
    start = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    stats = FakeStats(five={"sensor.load": five_min(start, 13)})  # 11:00 .. 12:05
    sync = HalfhourSync(hass, entry, FakeClient(), stats)
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.load", "cursor": start.isoformat()}}
    await sync.async_sync()
    sent = sync.client.sent[0]
    assert [(s["slot"][11:16], s["coverage_s"]) for s in sent] == [("11:00", 1800), ("11:30", 1800), ("12:00", 300)]
    # 11:00 is final (ended 11:30 + 10 min <= 12:07); 11:30 ends 12:00, final at 12:10: not yet.
    assert sync._cursors["house_load_w"]["cursor"] == "2026-09-28T11:30:00+00:00"
    assert sync.connected and sync.last_upload == now


async def test_first_run_backfills_hourly_then_hands_over_to_five_minute_without_overlap(hass, now, freezer):
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    handover = datetime(2026, 9, 17, 12, 5, tzinfo=UTC)  # 5-minute stats begin mid-hour
    hourly = [Period(handover.replace(minute=0) - timedelta(hours=h), 3600, 300.0, None) for h in range(0, 24 * 12)]
    stats = FakeStats(hourly={"sensor.load": hourly}, five={"sensor.load": five_min(handover, 12 * 24 * 11)})
    client = FakeClient()
    sync = HalfhourSync(hass, entry, client, stats)
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.load", "cursor": "2026-09-14T00:00:00+00:00"}}
    for _ in range(4):
        await sync.async_sync()
        freezer.tick(timedelta(seconds=11))  # requests are paced SEND_GAP apart
    assert len(client.sent) == 4
    await sync.async_stop()
    by_slot: dict[str, list[dict]] = {}
    for s in (s for batch in client.sent for s in batch):
        by_slot.setdefault(s["slot"], []).append(s)
    # Only slots not yet final at the end (at 12:07: 11:30 and 12:00) may be sent again.
    assert {k for k, v in by_slot.items() if len(v) > 1} <= {"2026-09-28T11:30:00+00:00", "2026-09-28T12:00:00+00:00"}
    # 11:00-12:00 on handover day is hourly; the 12:00 hour overlaps 5-minute
    # data (from 12:05), so its hourly row is dropped, not double-counted.
    assert by_slot["2026-09-17T11:30:00+00:00"][0]["resolution_s"] == 3600
    handover_slot = by_slot["2026-09-17T12:00:00+00:00"][0]
    assert (handover_slot["resolution_s"], handover_slot["coverage_s"]) == (300, 1500)
    assert by_slot["2026-09-17T12:30:00+00:00"][0]["resolution_s"] == 300


async def test_energy_counter_syncs_across_windows(hass, now, freezer):
    hass.states.async_set("sensor.energy", "1", {"state_class": "total_increasing", "unit_of_measurement": "kWh"})
    entry = load_entry(hass, {"house_load_w": {"entity_id": "sensor.energy", "invert": False, "kind": "energy"}})
    first = datetime(2026, 9, 28, 10, 55, tzinfo=UTC)  # the period just before the cursor
    # 0.05 kWh per 5 minutes = 600 W, from 10:55 through 12:35.
    rows = [Period(first + timedelta(minutes=5 * i), 300, None, 0.05 * (i + 1)) for i in range(21)]
    client = FakeClient()
    sync = HalfhourSync(hass, entry, client, FakeStats(five={"sensor.energy": rows}))
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.energy", "cursor": "2026-09-28T11:00:00+00:00"}}
    await sync.async_sync()
    got = {s["slot"][11:16]: (s["value"], s["coverage_s"]) for s in client.sent[0]}
    assert got["11:00"] == (600.0, 1800)  # the 11:00 period is not dropped: prev_sum is 10:55's
    assert sync.cursors["house_load_w"]["cursor"] == "2026-09-28T11:30:00+00:00"
    freezer.tick(timedelta(minutes=30))
    await sync.async_sync()
    got = {s["slot"][11:16]: (s["value"], s["coverage_s"]) for s in client.sent[1]}
    # The next window starts at the stored cursor with prev_sum from 11:25: no spike, nothing lost.
    assert got["11:30"] == (600.0, 1800) and got["12:00"] == (600.0, 1800)
    assert all(v == 600.0 for v, _ in got.values())


async def test_each_request_is_one_window_and_the_backlog_drains(hass, now, freezer):
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    hourly = [Period(datetime(2025, 8, 25, tzinfo=UTC) + timedelta(hours=h), 3600, 300.0, None) for h in range(24 * 400)]
    client = FakeClient()
    sync = HalfhourSync(hass, entry, client, FakeStats(hourly={"sensor.load": hourly}))
    await sync.async_load()
    await sync.async_sync()
    assert len(client.sent) == 1 and len(client.sent[0]) <= 5 * 48
    freezer.tick(timedelta(seconds=12))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(client.sent) == 2  # scheduled DRAIN_DELAY later while behind
    await sync.async_stop()


def backfill(hass, start: datetime, hours: int) -> tuple[MockConfigEntry, FakeStats]:
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    hourly = [Period(start + timedelta(hours=h), 3600, 300.0, None) for h in range(hours)]
    return entry, FakeStats(hourly={"sensor.load": hourly})


async def test_backfill_is_contiguous_when_the_first_cursor_is_on_half_past(hass, freezer):
    now = datetime(2026, 9, 28, 12, 37, tzinfo=UTC)  # now - MAX_AGE (360 d) floors to 12:30
    freezer.move_to(now)
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    first_hour = datetime(2025, 9, 1, 0, 0, tzinfo=UTC)
    handover = datetime(2026, 9, 18, 12, 5, tzinfo=UTC)
    hourly = [Period(first_hour + timedelta(hours=h), 3600, 300.0, None) for h in range(int((now - first_hour).total_seconds() // 3600))]
    stats = FakeStats(hourly={"sensor.load": hourly}, five={"sensor.load": five_min(handover, int((now - handover).total_seconds() // 300))})
    client = FakeClient()
    sync = HalfhourSync(hass, entry, client, stats)
    await sync.async_load()
    for _ in range(90):
        await sync.async_sync()
        freezer.tick(timedelta(seconds=11))
    await sync.async_stop()
    latest = {datetime.fromisoformat(s["slot"]): s for batch in client.sent for s in batch}
    slots = sorted(latest)
    assert slots[0] == datetime(2025, 10, 3, 12, 30, tzinfo=UTC)  # the half-past cursor slot itself
    missing = [slots[0] + i * timedelta(minutes=30) for i in range(int((slots[-1] - slots[0]) / timedelta(minutes=30)))]
    assert [t for t in missing if t not in latest] == []
    before = [latest[t] for t in slots if t < handover.replace(minute=0)]
    assert all(s["coverage_s"] == 1800 for s in before)


async def test_stop_during_a_send_stops_the_drain(hass, now, freezer):
    entry, stats = backfill(hass, datetime(2025, 10, 3, tzinfo=UTC), 24 * 30)

    class StoppingClient(FakeClient):
        async def send(self, slots):
            await sync.async_stop()  # unload/reload while the request is in flight
            return await super().send(slots)

    client = StoppingClient()
    sync = HalfhourSync(hass, entry, client, stats)
    await sync.async_load()
    await sync.async_sync()
    freezer.tick(timedelta(seconds=12))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(client.sent) == 1


async def test_a_tick_during_a_pending_drain_sends_nothing(hass, now, freezer):
    entry, stats = backfill(hass, datetime(2025, 10, 3, tzinfo=UTC), 24 * 30)
    client = FakeClient()
    sync = HalfhourSync(hass, entry, client, stats)
    await sync.async_load()
    await sync.async_sync()  # behind: a drain is due DRAIN_DELAY later
    freezer.tick(timedelta(seconds=3))
    await sync._on_tick(now)
    await sync.async_sync()  # even called directly, never within SEND_GAP of the last request
    assert len(client.sent) == 1
    freezer.tick(timedelta(seconds=8))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(client.sent) == 2
    await sync.async_stop()


async def test_empty_windows_are_skipped_through_in_one_sync(hass, now):
    data_from = datetime(2026, 8, 30, tzinfo=UTC)  # 60 days after the cursor
    entry, stats = backfill(hass, data_from, 24)
    client = FakeClient()
    sync = HalfhourSync(hass, entry, client, stats)
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.load", "cursor": "2026-07-01T00:00:00+00:00"}}
    await sync.async_sync()
    assert len(client.sent) == 1
    assert client.sent[0][0]["slot"] == data_from.isoformat() and len(client.sent[0]) == 48
    assert sync.cursors["house_load_w"]["cursor"] == "2026-09-04T00:00:00+00:00"
    await sync.async_stop()


async def test_remapping_a_role_backfills_it_again(hass, now):
    hass.states.async_set("sensor.new_load", "1", {"state_class": "measurement"})
    entry = load_entry(hass, {"house_load_w": {"entity_id": "sensor.new_load", "invert": False, "kind": "power"}})
    stats = FakeStats()
    sync = HalfhourSync(hass, entry, FakeClient(), stats)
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.old_load", "cursor": "2026-09-28T11:30:00+00:00"}}
    await sync.async_sync()
    first_start = min(start for _, _, start, _ in stats.asked)
    assert first_start <= NOW - timedelta(days=359)


async def test_a_fresh_role_backfills_360_days_well_inside_the_servers_window(hass, now):
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    stats = FakeStats()
    sync = HalfhourSync(hass, entry, FakeClient(), stats)
    await sync.async_load()
    await sync.async_sync()
    first_start = min(start for _, _, start, _ in stats.asked)
    assert first_start == slot_floor(NOW - timedelta(days=360)) == datetime(2025, 10, 3, 12, 0, tzinfo=UTC)
    # The gateway and optimiser accept slot starts back to 400 days.
    assert first_start > NOW - timedelta(days=400) + timedelta(days=30)
    await sync.async_stop()


@pytest.mark.parametrize("change", [{"invert": True}, {"kind": "energy"}])
async def test_changing_invert_or_kind_backfills_the_role_again(hass, now, change):
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, {"house_load_w": {**LOAD["house_load_w"], **change}})
    stats = FakeStats()
    sync = HalfhourSync(hass, entry, FakeClient(), stats)
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.load", "invert": False, "kind": "power", "cursor": "2026-09-28T11:30:00+00:00"}}
    await sync.async_sync()
    assert min(start for _, _, start, _ in stats.asked) == slot_floor(NOW - timedelta(days=360))
    assert sync.cursors["house_load_w"] | change == sync.cursors["house_load_w"]  # the new settings are stored
    await sync.async_stop()


async def test_the_same_invert_and_kind_keep_the_cursor(hass, now):
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    stats = FakeStats()
    sync = HalfhourSync(hass, entry, FakeClient(), stats)
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.load", "invert": False, "kind": "power", "cursor": "2026-09-28T11:30:00+00:00"}}
    await sync.async_sync()
    assert min(start for _, _, start, _ in stats.asked) >= datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    await sync.async_stop()


async def test_ten_roles_never_send_more_than_max_batch_slots(hass, now, freezer):
    mapping = {f"role_{i}": {"entity_id": f"sensor.r{i}", "invert": False, "kind": "power"} for i in range(10)}
    start = datetime(2025, 9, 1, tzinfo=UTC)
    hourly = {f"sensor.r{i}": [Period(start + timedelta(hours=h), 3600, 300.0, None) for h in range(24 * 60)] for i in range(10)}
    for i in range(10):
        hass.states.async_set(f"sensor.r{i}", "1", {"state_class": "measurement"})
    entry = load_entry(hass, mapping)
    client = FakeClient()
    sync = HalfhourSync(hass, entry, client, FakeStats(hourly=hourly))
    await sync.async_load()
    sync._cursors = {r: {"entity_id": m["entity_id"], "invert": False, "kind": "power", "cursor": start.isoformat()} for r, m in mapping.items()}
    for _ in range(3):
        await sync.async_sync()
        freezer.tick(timedelta(seconds=SEND_GAP))
    await sync.async_stop()
    assert len(client.sent) == 3
    assert all(0 < len(batch) <= MAX_BATCH for batch in client.sent), [len(b) for b in client.sent]
    # Contiguous: every role's slots across the three requests have no holes.
    for r in mapping:
        got = sorted({datetime.fromisoformat(s["slot"]) for b in client.sent for s in b if s["role"] == r})
        assert got[0] == start and got == [start + i * timedelta(minutes=30) for i in range(len(got))]


async def test_pacing_is_measured_from_the_request_not_the_recorder_reads(hass, now, freezer):
    assert SEND_GAP == 11  # the gateway allows one request per 10 s; DRAIN_DELAY is 11
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    start = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    inner = FakeStats(five={"sensor.load": five_min(start, 13)})

    async def slow(*args):
        freezer.tick(timedelta(seconds=4))  # a slow recorder read
        return await inner(*args)

    sent_at = []

    class TimedClient(FakeClient):
        async def send(self, slots):
            sent_at.append(datetime.now(UTC))
            return await super().send(slots)

    client = TimedClient()
    sync = HalfhourSync(hass, entry, client, slow)
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.load", "cursor": start.isoformat()}}
    await sync.async_sync()
    assert sync._last_send == sent_at[0] > now
    freezer.move_to(sent_at[0] + timedelta(seconds=SEND_GAP - 0.5))
    await sync.async_sync()
    assert len(client.sent) == 1  # still inside SEND_GAP of the request itself
    await sync.async_stop()


async def test_cursors_survive_a_restart(hass, now):
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    start = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    stats = FakeStats(five={"sensor.load": five_min(start, 13)})
    a = HalfhourSync(hass, entry, FakeClient(), stats)
    await a.async_load()
    a._cursors = {"house_load_w": {"entity_id": "sensor.load", "cursor": start.isoformat()}}
    await a.async_sync()
    await a.async_stop()
    b = HalfhourSync(hass, entry, FakeClient(), stats)
    await b.async_load()
    assert b._cursors["house_load_w"]["cursor"] == "2026-09-28T11:30:00+00:00"


async def test_rejected_batch_moves_on(hass, now):
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    start = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    sync = HalfhourSync(hass, entry, FakeClient(RejectedError("bad")), FakeStats(five={"sensor.load": five_min(start, 13)}))
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.load", "cursor": start.isoformat()}}
    await sync.async_sync()
    assert sync._cursors["house_load_w"]["cursor"] == "2026-09-28T11:30:00+00:00"


async def test_retry_later_keeps_the_cursor_backs_off_and_logs_once(hass, now, freezer, caplog):
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    start = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    client = FakeClient(RetryLater(120), RetryLater(None))
    sync = HalfhourSync(hass, entry, client, FakeStats(five={"sensor.load": five_min(start, 13)}))
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.load", "cursor": start.isoformat()}}
    await sync.async_sync()
    assert sync._cursors["house_load_w"]["cursor"] == start.isoformat() and not sync.connected
    await sync.async_sync()  # inside the backoff: ignored
    assert len(client.sent) == 1
    freezer.tick(timedelta(seconds=121))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert sync.connected
    assert caplog.text.count("unreachable") == 1 and caplog.text.count("reachable again") == 1


async def test_auth_error_starts_reauth_and_stops(hass, now):
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    start = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    client = FakeClient(AuthError("x"))
    sync = HalfhourSync(hass, entry, client, FakeStats(five={"sensor.load": five_min(start, 13)}))
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.load", "cursor": start.isoformat()}}
    with patch.object(entry, "async_start_reauth") as reauth:
        await sync.async_sync()
        await sync.async_sync()
    reauth.assert_called_once()
    assert len(client.sent) == 1


def issue(hass, entry, role="house_load_w"):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_no_statistics_{role}")


async def test_a_sensor_without_statistics_raises_a_repair_issue_until_fixed(hass, now):
    hass.states.async_set("sensor.load", "400", {})
    entry = load_entry(hass, LOAD)
    sync = HalfhourSync(hass, entry, FakeClient(), FakeStats())
    await sync.async_load()
    await sync.async_sync()
    assert issue(hass, entry)
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    await sync.async_sync()
    assert issue(hass, entry) is None


async def test_no_repair_issue_before_home_assistant_has_started(hass, now):
    """Restored and MQTT entities may not exist yet during startup."""
    entry = load_entry(hass, LOAD)  # sensor.load has no state yet
    sync = HalfhourSync(hass, entry, FakeClient(), FakeStats())
    await sync.async_load()
    hass.set_state(CoreState.not_running)
    try:
        await sync.async_sync()
        assert issue(hass, entry) is None
    finally:
        hass.set_state(CoreState.running)
    sync._last_send = None
    await sync.async_sync()
    assert issue(hass, entry)


async def test_statistics_are_judged_from_the_entity_registry_when_registered(hass, now):
    registry = er.async_get(hass)
    ok = registry.async_get_or_create("sensor", "test", "ok", suggested_object_id="load", capabilities={"state_class": "measurement"})
    bad = registry.async_get_or_create("sensor", "test", "bad", suggested_object_id="bad")
    hass.states.async_set(bad.entity_id, "1", {"state_class": "measurement"})  # a stale live state doesn't count
    entry = load_entry(hass, {"house_load_w": {"entity_id": ok.entity_id, "invert": False, "kind": "power"}, "solar_w": {"entity_id": bad.entity_id, "invert": False, "kind": "power"}})
    sync = HalfhourSync(hass, entry, FakeClient(), FakeStats())
    await sync.async_load()
    await sync.async_sync()  # ok has no state at all, but its registry entry has a state_class
    assert issue(hass, entry) is None
    assert issue(hass, entry, "solar_w")


async def test_issues_are_per_entry_and_cleared_for_unmapped_roles(hass, now):
    entry = load_entry(hass, {**LOAD, "solar_w": {"entity_id": "sensor.solar", "invert": False, "kind": "power"}})
    other = MockConfigEntry(domain=DOMAIN, unique_id="hub-2", data={CONF_URL: "https://hh.test", CONF_TOKEN: "t", CONF_HUB_ID: "hub-2"}, options={CONF_MAPPING: LOAD, CONF_ROLES: []})
    other.add_to_hass(hass)
    sync = HalfhourSync(hass, entry, FakeClient(), FakeStats())
    await sync.async_load()
    await sync.async_sync()
    assert issue(hass, entry, "solar_w") and issue(hass, entry)
    ir.async_create_issue(hass, DOMAIN, f"{other.entry_id}_no_statistics_solar_w", is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key="no_statistics")
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_MAPPING: LOAD})
    sync._last_send = None
    await sync.async_sync()
    assert issue(hass, entry, "solar_w") is None and issue(hass, entry)
    assert issue(hass, other, "solar_w")  # another home's issues are its own


# -- the cursors belong to one hub ---------------------------------------------

CURSORS = {"house_load_w": {"entity_id": "sensor.load", "invert": False, "kind": "power", "cursor": "2026-09-28T11:30:00+00:00"}}
LEGACY = {"house_load_w": {"entity_id": "sensor.load", "cursor": "2026-09-28T11:30:00+00:00"}}


def cursor_store(hass, entry):
    return Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.cursors")


async def test_cursors_are_saved_with_the_hub(hass, now):
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    start = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    sync = HalfhourSync(hass, entry, FakeClient(), FakeStats(five={"sensor.load": five_min(start, 13)}))
    await sync.async_load()
    sync._cursors = {"house_load_w": {"entity_id": "sensor.load", "cursor": start.isoformat()}}
    await sync.async_sync()
    await sync.async_stop()
    saved = await cursor_store(hass, entry).async_load()
    assert saved == {"hub_id": "hub-1", "cursors": CURSORS}


async def test_a_new_hub_drops_the_cursors(hass):
    entry = load_entry(hass, LOAD)
    await cursor_store(hass, entry).async_save({"hub_id": "hub-0", "cursors": CURSORS})
    sync = HalfhourSync(hass, entry, FakeClient(), FakeStats())
    await sync.async_load()
    assert sync.cursors == {}


async def test_the_same_hub_keeps_the_cursors(hass):
    entry = load_entry(hass, LOAD)
    await cursor_store(hass, entry).async_save({"hub_id": "hub-1", "cursors": CURSORS})
    sync = HalfhourSync(hass, entry, FakeClient(), FakeStats())
    await sync.async_load()
    assert sync.cursors == CURSORS


async def test_old_flat_cursors_are_kept(hass):
    """0.2.0 before this fix saved bare cursors: assume they are this hub's."""
    entry = load_entry(hass, LOAD)
    await cursor_store(hass, entry).async_save(LEGACY)
    sync = HalfhourSync(hass, entry, FakeClient(), FakeStats())
    await sync.async_load()
    assert sync.cursors == LEGACY


async def test_cursors_without_invert_or_kind_are_kept(hass, now):
    """0.2.0 cursors carry no invert/kind: take them as the current mapping's, not a reset."""
    hass.states.async_set("sensor.load", "400", {"state_class": "measurement"})
    entry = load_entry(hass, LOAD)
    await cursor_store(hass, entry).async_save({"hub_id": "hub-1", "cursors": LEGACY})
    stats = FakeStats()
    sync = HalfhourSync(hass, entry, FakeClient(), stats)
    await sync.async_load()
    await sync.async_sync()
    assert min(start for _, _, start, _ in stats.asked) >= datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    await sync.async_stop()
