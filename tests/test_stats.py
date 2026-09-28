"""The recorder boundary: statistics rows in, Periods out."""

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done, do_adhoc_statistics

from custom_components.halfhour.stats import _utc, fetch, has_statistics

POWER_ATTRS = {"state_class": "measurement", "unit_of_measurement": "W", "device_class": "power"}


async def test_fetch_five_minute_power_means(recorder_mock: Recorder, hass: HomeAssistant, freezer) -> None:
    assert await async_setup_component(hass, "sensor", {})  # its recorder platform compiles the statistics
    t0 = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    freezer.move_to(t0)
    hass.states.async_set("sensor.load", "400", POWER_ATTRS)
    await async_wait_recording_done(hass)
    freezer.move_to(t0 + timedelta(minutes=5))
    hass.states.async_set("sensor.load", "800", POWER_ATTRS)
    await async_wait_recording_done(hass)
    freezer.move_to(t0 + timedelta(minutes=10))
    hass.states.async_set("sensor.load", "801", POWER_ATTRS)
    await async_wait_recording_done(hass)
    freezer.move_to(t0 + timedelta(minutes=11))

    do_adhoc_statistics(hass, period="5minute", start=t0)
    do_adhoc_statistics(hass, period="5minute", start=t0 + timedelta(minutes=5))
    await async_wait_recording_done(hass)

    rows = await fetch(hass, "sensor.load", "power", t0, t0 + timedelta(minutes=10), "5minute")
    assert [p.start for p in rows] == [t0, t0 + timedelta(minutes=5)]
    assert all(p.start.tzinfo is not None and p.start.utcoffset() == timedelta(0) for p in rows)
    assert [p.seconds for p in rows] == [300, 300]
    assert rows[0].mean == pytest.approx(400.0)
    assert rows[1].mean == pytest.approx(800.0)
    assert await has_statistics(hass, "sensor.load")


async def test_fetch_hourly_energy_sums(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    start = datetime(2025, 9, 28, 0, 0, tzinfo=UTC)
    n = 24 * 365
    async_import_statistics(
        hass,
        {
            "has_mean": False,
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": "sensor.energy",
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [{"start": start + timedelta(hours=i), "sum": 0.5 * (i + 1), "state": 0.5 * (i + 1)} for i in range(n)],
    )
    await async_wait_recording_done(hass)

    rows = await fetch(hass, "sensor.energy", "energy", start, start + timedelta(hours=n), "hour")
    assert len(rows) == n
    assert rows[0].start == start
    assert rows[-1].start == start + timedelta(hours=n - 1)
    assert {p.seconds for p in rows} == {3600}
    assert rows[0].sum == pytest.approx(0.5)
    assert rows[-1].sum == pytest.approx(0.5 * n)

    # [start, end): a window ends before the row starting at end.
    part = await fetch(hass, "sensor.energy", "energy", start, start + timedelta(hours=2), "hour")
    assert [p.start for p in part] == [start, start + timedelta(hours=1)]

    assert await has_statistics(hass, "sensor.energy")
    assert not await has_statistics(hass, "sensor.nothing")


async def test_fetch_percent_means_unconverted(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    start = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    async_import_statistics(
        hass,
        {
            "has_mean": True,
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
            "name": None,
            "source": "recorder",
            "statistic_id": "sensor.soc",
            "unit_class": None,
            "unit_of_measurement": "%",
        },
        [{"start": start + timedelta(hours=i), "mean": 50.0 + i, "min": 50.0, "max": 60.0} for i in range(3)],
    )
    await async_wait_recording_done(hass)

    rows = await fetch(hass, "sensor.soc", "percent", start, start + timedelta(hours=3), "hour")
    assert [p.mean for p in rows] == [pytest.approx(50.0), pytest.approx(51.0), pytest.approx(52.0)]
    assert all(p.sum is None for p in rows)


def test_row_start_may_be_a_datetime() -> None:
    aware = datetime(2026, 9, 1, 1, 0, tzinfo=dt_util.get_time_zone("Europe/London"))
    assert _utc(aware) == datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    assert _utc(datetime(2026, 9, 1, 0, 0)) == datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
