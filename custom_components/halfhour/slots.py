"""Turn HA recorder statistics into the half-hour slots Halfhour stores.

All the arithmetic lives here, in the home, so the backend only checks and
stores what arrives. A slot's value is the average W (or %) over the part
of the half hour that HA has statistics for; coverage_s says how much.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

SLOT = timedelta(minutes=30)
SLOT_S = 1800
Kind = Literal["energy", "power", "percent"]


@dataclass(frozen=True)
class Period:
    """One recorder statistics row: 5-minute or hourly."""

    start: datetime
    seconds: int
    mean: float | None
    sum: float | None


def slot_floor(t: datetime) -> datetime:
    return t - timedelta(minutes=t.minute % 30, seconds=t.second, microseconds=t.microsecond)


def _watts(kind: Kind, p: Period, prev_sum: float | None, prev_period_end: datetime | None) -> float | None:
    """A period's average in the role's unit, or None when it can't be known."""
    if kind != "energy":
        return p.mean
    if p.sum is None or prev_sum is None:
        return None
    # Check for gap: previous period should end exactly where this one starts
    if prev_period_end is not None and prev_period_end != p.start:
        return None  # Gap detected, treat as unknown (re-baseline from this period)
    delta = p.sum - prev_sum
    if delta < 0:
        return None  # counter reset or replaced meter: never invent energy
    return delta * 3_600_000 / p.seconds


def build_slots(role: str, kind: Kind, periods: list[Period], invert: bool, prev_sum: float | None) -> list[dict[str, Any]]:
    # slot start -> [(value, seconds, resolution)]
    parts: dict[datetime, list[tuple[float, int, int]]] = defaultdict(list)
    prev_period_end: datetime | None = None
    for p in periods:
        value = _watts(kind, p, prev_sum, prev_period_end)
        if kind == "energy":
            # The next delta is taken from this row. A row without a sum
            # breaks the chain (None), so the next row re-baselines instead
            # of spanning two periods divided by one period's seconds.
            prev_sum = p.sum
        prev_period_end = p.start + timedelta(seconds=p.seconds)
        if value is None:
            continue
        if invert and kind != "percent":
            value = -value
        if p.seconds > SLOT_S:
            # An hourly row covers two slots evenly: same value, marked coarse.
            base = slot_floor(p.start)
            for k in range(p.seconds // SLOT_S):
                parts[base + k * SLOT].append((value, SLOT_S, p.seconds))
        else:
            parts[slot_floor(p.start)].append((value, p.seconds, p.seconds))
    out: list[dict[str, Any]] = []
    for start in sorted(parts):
        rows = parts[start]
        coverage = min(SLOT_S, sum(s for _, s, _ in rows))
        value = sum(v * s for v, s, _ in rows) / sum(s for _, s, _ in rows)
        out.append(
            {
                "slot": start.isoformat(),
                "role": role,
                "value": round(value, 3),
                "coverage_s": coverage,
                "resolution_s": max(r for _, _, r in rows),
            }
        )
    return out
