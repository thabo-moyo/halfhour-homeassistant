"""The recorder boundary: read long-term statistics as Periods."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from homeassistant.components.recorder.statistics import async_list_statistic_ids, statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance

from .slots import Kind, Period

_SECONDS: dict[str, int] = {"5minute": 300, "hour": 3600}
_Type = Literal["change", "last_reset", "max", "mean", "min", "state", "sum"]


def _query(kind: Kind) -> tuple[dict[str, str] | None, set[_Type]]:
    """The units to convert into and the columns to read, per kind."""
    if kind == "energy":
        return {"energy": "kWh"}, {"sum"}
    if kind == "power":
        return {"power": "W"}, {"mean"}
    return None, {"mean"}


def _utc(value: Any) -> datetime:
    """A row's start: a float epoch or a datetime, as an aware UTC datetime."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return datetime.fromtimestamp(float(value), UTC)


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


async def fetch(
    hass: HomeAssistant, entity_id: str, kind: Kind, start: datetime, end: datetime, period: Literal["5minute", "hour"]
) -> list[Period]:
    """The recorder's rows for entity_id with start <= row start < end."""
    units, types = _query(kind)
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {entity_id}, period, units, types
    )
    seconds = _SECONDS[period]
    return [
        Period(_utc(row["start"]), seconds, _float(row.get("mean")), _float(row.get("sum")))
        for row in result.get(entity_id, [])
    ]


async def has_statistics(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether the recorder keeps (or is about to keep) statistics for entity_id."""
    return bool(await async_list_statistic_ids(hass, statistic_ids={entity_id}))
