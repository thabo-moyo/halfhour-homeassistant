"""Turn a sensor's state into a role's canonical value, or nothing."""

from __future__ import annotations

import math

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

# Power units a W role accepts, and their factor to watts.
POWER_UNITS = {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0}


def canonical(state: State | None, unit: str, invert: bool) -> float | None:
    """Return state as a finite value in `unit` (W or %), sign-corrected.

    None means "send nothing": unavailable, non-numeric, or a unit that
    cannot be converted. A missing unit is taken as already canonical.
    """
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    have = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit == "W":
        factor = POWER_UNITS.get(have or "W")
        if factor is None:
            return None
        value *= factor
    elif have not in (None, "%"):
        return None
    return -value if invert else value
