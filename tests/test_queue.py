"""The disk-backed sample queue."""

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant

from custom_components.halfhour.queue import SampleQueue


def sample(ts: datetime) -> dict:
    return {"ts": ts.isoformat(), "role": "house_load_w", "value": 1.0}


async def test_queue_is_fifo_and_survives_reload(hass: HomeAssistant, hass_storage):
    t0 = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
    q = SampleQueue(hass, "entry1")
    await q.async_load()
    q.add([sample(t0), sample(t0 + timedelta(minutes=1)), sample(t0 + timedelta(minutes=2))])
    assert [s["ts"] for s in q.peek(2)] == [sample(t0)["ts"], sample(t0 + timedelta(minutes=1))["ts"]]
    q.drop(1)
    await q.async_flush()

    again = SampleQueue(hass, "entry1")
    await again.async_load()
    assert len(again) == 2
    assert again.peek(1)[0]["ts"] == sample(t0 + timedelta(minutes=1))["ts"]


async def test_queue_expires_samples_older_than_seven_days(hass: HomeAssistant, hass_storage):
    now = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
    q = SampleQueue(hass, "entry2")
    await q.async_load()
    q.add([sample(now - timedelta(days=7, minutes=1)), sample(now - timedelta(days=6))])
    q.expire(now)
    assert len(q) == 1


async def test_remove_deletes_the_file(hass: HomeAssistant, hass_storage):
    q = SampleQueue(hass, "entry3")
    await q.async_load()
    q.add([sample(datetime(2026, 9, 27, tzinfo=UTC))])
    await q.async_flush()
    assert "halfhour.entry3.queue" in hass_storage
    await q.async_remove()
    assert "halfhour.entry3.queue" not in hass_storage
