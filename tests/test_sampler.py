"""Sampling mapped entities."""

from datetime import UTC, datetime

from homeassistant.core import HomeAssistant

from custom_components.halfhour.sampler import collect

MAPPING = {
    "house_load_w": {"entity_id": "sensor.load", "invert": False, "unit": "W"},
    "battery_power_w": {"entity_id": "sensor.batt", "invert": True, "unit": "W"},
    "solar_w": {"entity_id": "sensor.pv", "invert": False, "unit": "W"},
    "battery_soc_pct": {"entity_id": "sensor.missing", "invert": False, "unit": "%"},
}


async def test_collect_normalises_and_skips_unusable(hass: HomeAssistant):
    hass.states.async_set("sensor.load", "0.45", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.batt", "-1500", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.pv", "unavailable")
    now = datetime(2026, 9, 27, 10, 3, 42, 123000, tzinfo=UTC)

    got = {s["role"]: s for s in collect(hass, MAPPING, now)}

    assert set(got) == {"house_load_w", "battery_power_w"}, "unavailable and missing entities send nothing"
    assert got["house_load_w"]["value"] == 450.0
    assert got["battery_power_w"]["value"] == 1500.0
    assert got["house_load_w"]["ts"] == "2026-09-27T10:03:00+00:00", "aligned to the UTC minute"
    assert got["house_load_w"]["reported_at"] == hass.states.get("sensor.load").last_reported.isoformat()
