"""Read every mapped entity once: one sample per usable role."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .normalize import canonical


def collect(hass: HomeAssistant, mapping: dict[str, dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Sample each role's entity at `now` (a UTC datetime).

    ts is aligned to the minute so a resent sample overwrites its stored
    row. reported_at is the state's last_reported, which advances even when
    the value repeats (last_updated would not).
    """
    ts = now.replace(second=0, microsecond=0).isoformat()
    out: list[dict[str, Any]] = []
    for role, m in mapping.items():
        state = hass.states.get(m["entity_id"])
        value = canonical(state, m["unit"], bool(m.get("invert")))
        if value is None or state is None:
            continue
        out.append({"ts": ts, "role": role, "value": value, "reported_at": state.last_reported.isoformat()})
    return out
