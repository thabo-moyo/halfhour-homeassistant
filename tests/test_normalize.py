"""Unit and sign normalisation."""

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

from custom_components.halfhour.normalize import canonical


def s(value: str, unit: str | None = "W") -> State:
    attrs = {"unit_of_measurement": unit} if unit else {}
    return State("sensor.x", value, attrs)


def test_watts_pass_through_and_kilowatts_scale():
    assert canonical(s("412.5"), "W", False) == 412.5
    assert canonical(s("1.2", "kW"), "W", False) == 1200.0
    assert canonical(s("0.002", "MW"), "W", False) == 2000.0


def test_missing_unit_is_taken_as_canonical():
    assert canonical(s("300", None), "W", False) == 300.0
    assert canonical(s("80", None), "%", False) == 80.0


def test_invert_flips_sign():
    assert canonical(s("-2500"), "W", True) == 2500.0


def test_unusable_states_are_dropped_not_sent():
    assert canonical(None, "W", False) is None
    assert canonical(s(STATE_UNAVAILABLE), "W", False) is None
    assert canonical(s(STATE_UNKNOWN), "W", False) is None
    assert canonical(s("on"), "W", False) is None
    assert canonical(s("nan"), "W", False) is None
    assert canonical(s("inf"), "W", False) is None
    assert canonical(s("5", "A"), "W", False) is None, "amps are not watts"
    assert canonical(s("80", "kWh"), "%", False) is None
