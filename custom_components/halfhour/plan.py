"""The Plan model: what the Optimiser publishes over MQTT, parsed and validated.

Pure and side-effect free — no HA, no MQTT, no storage. Newest-wins is a
`made_at` comparison; the covering slot and staleness checks are what the
sensor layer needs to decide what to show and when to stop trusting it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

_VALID_SLOT_MINUTES = (5, 15, 30, 60)

_PLAN_FIELDS = ("v", "id", "account", "made_at", "slot_minutes", "slots")
_SLOT_FIELDS = ("start", "import_p", "export_p", "load_w", "pv_w", "battery_w", "grid_w", "soc")


class PlanError(ValueError):
    """The plan payload is malformed, unsupported, or otherwise unusable."""


@dataclass(frozen=True)
class PlanSlot:
    start: datetime
    end: datetime
    import_p: float
    export_p: float
    load_w: float | None
    pv_w: float
    battery_w: float | None
    grid_w: float | None
    soc: float | None


@dataclass(frozen=True)
class Plan:
    id: str
    account: str
    made_at: datetime
    slot_minutes: int
    slots: tuple[PlanSlot, ...]


def _require_aware(dt: datetime, what: str) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise PlanError(f"{what} must be timezone-aware")
    return dt


def _parse_datetime(value: Any, what: str) -> datetime:
    if not isinstance(value, str):
        raise PlanError(f"{what} must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise PlanError(f"{what} is not a valid RFC 3339 timestamp: {value!r}") from err
    return _require_aware(parsed, what)


def _require_field(obj: dict[str, Any], field: str, what: str) -> Any:
    if field not in obj:
        raise PlanError(f"{what} is missing field {field!r}")
    return obj[field]


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{what} must be a number")
    if not math.isfinite(value):
        raise PlanError(f"{what} must be finite")
    return float(value)


def _optional_number(value: Any, what: str) -> float | None:
    if value is None:
        return None
    return _number(value, what)


def _parse_slot(raw: Any, slot_minutes: int, index: int) -> PlanSlot:
    if not isinstance(raw, dict):
        raise PlanError(f"slots[{index}] must be an object")
    for field in _SLOT_FIELDS:
        _require_field(raw, field, f"slots[{index}]")
    start = _parse_datetime(raw["start"], f"slots[{index}].start")
    return PlanSlot(
        start=start,
        end=start + timedelta(minutes=slot_minutes),
        import_p=_number(raw["import_p"], f"slots[{index}].import_p"),
        export_p=_number(raw["export_p"], f"slots[{index}].export_p"),
        load_w=_optional_number(raw["load_w"], f"slots[{index}].load_w"),
        pv_w=_number(raw["pv_w"], f"slots[{index}].pv_w"),
        battery_w=_optional_number(raw["battery_w"], f"slots[{index}].battery_w"),
        grid_w=_optional_number(raw["grid_w"], f"slots[{index}].grid_w"),
        soc=_optional_number(raw["soc"], f"slots[{index}].soc"),
    )


def _parse_obj(data: Any) -> Plan:
    """Validate an already-decoded plan object (from JSON or from Store) into a Plan.

    Shared by `parse_plan` (wire payload, after `json.loads`) and `from_store`
    (Store payload, already a dict): both must reject the same malformed and
    corrupt shapes, since a corrupt Store entry is exactly as untrustworthy as
    a corrupt wire payload.
    """
    if not isinstance(data, dict):
        raise PlanError("plan payload must be a JSON object")
    for field in _PLAN_FIELDS:
        _require_field(data, field, "plan")

    if data["v"] != 1:
        raise PlanError(f"unsupported plan version: {data['v']!r}")

    slot_minutes = data["slot_minutes"]
    if not isinstance(slot_minutes, int) or isinstance(slot_minutes, bool) or slot_minutes not in _VALID_SLOT_MINUTES:
        raise PlanError(f"slot_minutes must be one of {_VALID_SLOT_MINUTES}, got {slot_minutes!r}")

    plan_id = data["id"]
    account = data["account"]
    if not isinstance(plan_id, str) or not isinstance(account, str):
        raise PlanError("plan id and account must be strings")

    made_at = _parse_datetime(data["made_at"], "made_at")

    raw_slots = data["slots"]
    if not isinstance(raw_slots, list):
        raise PlanError("slots must be a list")
    slots = tuple(_parse_slot(raw, slot_minutes, i) for i, raw in enumerate(raw_slots))

    for i in range(1, len(slots)):
        if slots[i].start < slots[i - 1].end:
            raise PlanError(f"slots[{i}].start overlaps, duplicates or precedes slots[{i - 1}]")

    return Plan(id=plan_id, account=account, made_at=made_at, slot_minutes=slot_minutes, slots=slots)


def parse_plan(payload: bytes | str) -> Plan:
    """Parse and validate a wire plan payload. Raises PlanError on anything wrong."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise PlanError(f"invalid JSON: {err}") from err
    return _parse_obj(data)


def newer(current: Plan | None, incoming: Plan) -> bool:
    """True iff `incoming` should replace `current` (newest-wins on made_at)."""
    if current is None:
        return True
    return incoming.made_at > current.made_at


def covering_slot(plan: Plan, now: datetime) -> PlanSlot | None:
    """The slot whose [start, end) covers `now`, or None if outside the horizon."""
    for slot in plan.slots:
        if slot.start <= now < slot.end:
            return slot
    return None


def is_stale(plan: Plan | None, now: datetime, stale_after_s: int) -> bool:
    """True if there's no usable plan: missing, too old, or nothing covers `now`."""
    if plan is None:
        return True
    if (now - plan.made_at).total_seconds() > stale_after_s:
        return True
    if covering_slot(plan, now) is None:
        return True
    return False


def to_store(plan: Plan) -> dict[str, Any]:
    """Serialise a Plan into JSON-safe data for HA's Store, in the wire shape.

    Kept identical in shape to the wire payload (including `v`) so `from_store`
    can validate it through the exact same path as `parse_plan`.
    """
    return {
        "v": 1,
        "id": plan.id,
        "account": plan.account,
        "made_at": plan.made_at.isoformat(),
        "slot_minutes": plan.slot_minutes,
        "slots": [
            {
                "start": slot.start.isoformat(),
                "import_p": slot.import_p,
                "export_p": slot.export_p,
                "load_w": slot.load_w,
                "pv_w": slot.pv_w,
                "battery_w": slot.battery_w,
                "grid_w": slot.grid_w,
                "soc": slot.soc,
            }
            for slot in plan.slots
        ],
    }


def from_store(data: dict[str, Any]) -> Plan:
    """Rebuild a Plan from `to_store` data, validated exactly like `parse_plan`.

    A Store entry can be corrupted (partial write, manual edit, a future
    format) just like a wire payload can, so this raises PlanError on the
    same problems `parse_plan` catches, rather than trusting the shape and
    raising a bare KeyError/TypeError. Callers should treat PlanError here as
    "no usable stored plan".
    """
    return _parse_obj(data)
