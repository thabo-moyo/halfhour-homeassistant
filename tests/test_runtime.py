"""Upload behaviour: what happens to the queue on each gateway answer."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.halfhour.api import AuthError, RejectedError, RetryLater
from custom_components.halfhour.const import CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DOMAIN, MAX_AGE, MAX_BATCH
from custom_components.halfhour.queue import SampleQueue
from custom_components.halfhour.runtime import HalfhourRuntime


class FakeClient:
    """Answers send() from a script of results (int) or exceptions."""

    def __init__(self, *script):
        self.script = list(script)
        self.sent: list[list[dict]] = []

    async def send(self, samples):
        self.sent.append(list(samples))
        result = self.script.pop(0) if self.script else len(samples)
        if isinstance(result, Exception):
            raise result
        return result


def samples(n: int) -> list[dict]:
    now = dt_util.utcnow()
    return [{"ts": (now - timedelta(minutes=i)).isoformat(), "role": "house_load_w", "value": 1.0} for i in range(n)]


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    e = MockConfigEntry(
        domain=DOMAIN,
        unique_id="hub-1",
        data={CONF_URL: "https://hh.test", CONF_TOKEN: "t", CONF_HUB_ID: "hub-1"},
        options={CONF_MAPPING: {}, CONF_ROLES: []},
    )
    e.add_to_hass(hass)
    return e


async def make(hass, entry, client, n=0) -> HalfhourRuntime:
    q = SampleQueue(hass, entry.entry_id)
    await q.async_load()
    q.add(samples(n))
    return HalfhourRuntime(hass, entry, client, q)


async def advance(hass, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """Move the clock AND fire timers: the runtime compares against utcnow()."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_success_drains_one_batch_and_schedules_the_next(hass, entry, freezer):
    client = FakeClient()
    rt = await make(hass, entry, client, MAX_BATCH + 5)
    await rt.async_upload()
    assert [len(b) for b in client.sent] == [MAX_BATCH]
    assert len(rt.queue) == 5 and rt.connected and rt.last_upload is not None
    await advance(hass, freezer, 12)
    assert [len(b) for b in client.sent] == [MAX_BATCH, 5]
    assert len(rt.queue) == 0


async def test_rejected_batch_is_dropped(hass, entry):
    rt = await make(hass, entry, FakeClient(RejectedError("samples[0]: unknown role")), 3)
    await rt.async_upload()
    assert len(rt.queue) == 0


async def test_retry_later_keeps_batch_and_backs_off_honouring_retry_after(hass, entry, freezer):
    client = FakeClient(RetryLater(120), RetryLater(None))
    rt = await make(hass, entry, client, 3)
    await rt.async_upload()
    assert len(rt.queue) == 3 and not rt.connected
    # Backoff is max(30 s, Retry-After 120 s): nothing at +60 s.
    await advance(hass, freezer, 60)
    assert len(client.sent) == 1
    await advance(hass, freezer, 61)  # +121 s
    assert len(client.sent) == 2 and len(rt.queue) == 3
    # Second failure: 60 s (30 doubled), no Retry-After.
    await rt.async_upload()  # ignored while backing off
    assert len(client.sent) == 2
    await advance(hass, freezer, 61)
    assert len(client.sent) == 3 and len(rt.queue) == 0


async def test_auth_error_starts_reauth_and_stops_uploading(hass, entry):
    client = FakeClient(AuthError("x"))
    rt = await make(hass, entry, client, 3)
    with patch.object(entry, "async_start_reauth") as reauth:
        await rt.async_upload()
        await rt.async_upload()
    reauth.assert_called_once()
    assert len(client.sent) == 1 and len(rt.queue) == 3


async def test_queue_expires_while_auth_is_failed(hass, entry, freezer):
    """The queue must not grow without bound while reauth is pending (finding 2)."""
    hass.config_entries.async_update_entry(entry, options={CONF_MAPPING: {"house_load_w": {"entity_id": "sensor.load", "invert": False, "unit": "W"}}, CONF_ROLES: []})
    hass.states.async_set("sensor.load", "400", {"unit_of_measurement": "W"})
    client = FakeClient(AuthError("x"))
    rt = await make(hass, entry, client, 0)
    with patch.object(entry, "async_start_reauth"):
        rt.sample_now()
        await rt.async_upload()
    assert rt._auth_failed

    # 8 days of hourly sample ticks while auth stays failed and no upload succeeds.
    for _ in range(8 * 24):
        freezer.tick(timedelta(hours=1))
        rt.sample_now()

    cutoff = dt_util.utcnow() - MAX_AGE
    ages = [dt_util.parse_datetime(s["ts"]) for s in rt.queue.peek(len(rt.queue))]
    assert ages, "queue unexpectedly empty"
    assert all(t >= cutoff for t in ages), f"samples older than MAX_AGE survived: oldest={min(ages)} cutoff={cutoff}"
    assert len(rt.queue) <= 7 * 24 + 2


async def test_sample_now_adds_mapped_samples(hass, entry):
    hass.config_entries.async_update_entry(entry, options={CONF_MAPPING: {"house_load_w": {"entity_id": "sensor.load", "invert": False, "unit": "W"}}, CONF_ROLES: []})
    hass.states.async_set("sensor.load", "400", {"unit_of_measurement": "W"})
    rt = await make(hass, entry, FakeClient())
    rt.sample_now()
    assert len(rt.queue) == 1


async def test_start_sends_a_first_reading_straight_away(hass, entry):
    """A newly set-up home shows it's connected at once, not 5 minutes later."""
    hass.config_entries.async_update_entry(entry, options={CONF_MAPPING: {"house_load_w": {"entity_id": "sensor.load", "invert": False, "unit": "W"}}, CONF_ROLES: []})
    hass.states.async_set("sensor.load", "400", {"unit_of_measurement": "W"})
    client = FakeClient()
    rt = await make(hass, entry, client)
    rt.async_start()
    await hass.async_block_till_done()
    assert [len(b) for b in client.sent] == [1]
    assert rt.connected and rt.last_upload is not None and len(rt.queue) == 0
    await rt.async_stop()
