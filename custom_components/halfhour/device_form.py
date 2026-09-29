"""The add/edit device form, built from the kind schema Halfhour serves (GET /device-types, hub_kinds).

Nothing here knows a kind's fields: each one comes from the served schema.
A field is an entity picker filtered by the field's domains, plus a
"reversed" switch where the field allows invert; a fact is a number, time,
days or choice picker with the served range, unit and options.

Entity pickers filter by domain only, never by device class: the gateway
also accepts an entity with no device class when its unit is one the field
allows (many helpers and template sensors carry none), and a picker filter
can't say "this device class, or none with this unit". Filtering by device
class would hide entities the gateway takes; a wrong pick comes back as a
422 naming the field, shown on it.
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntityFilterSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TimeSelector,
)

# The "days" fact type's values, fixed by the contract (a non-empty subset of these).
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
NAME = "name"
LABEL = "label"  # what a load is (Dishwasher); the gateway keeps it for the "load" kind only
_LABELLED = "load"
INVERT = "_invert"  # suffix of a field's "reversed" switch, as on the sensors step
_FIRST_WORD = re.compile(r"^([a-z_]+)(?::\s*|\s)")


def valid_kinds(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The usable hub kinds of a /device-types body."""
    kinds = body.get("hub_kinds")
    if not isinstance(kinds, list):
        return []
    return [k for k in kinds if isinstance(k, dict) and isinstance(k.get("kind"), str) and isinstance(k.get("fields"), list)]


def find_kind(kinds: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((k for k in kinds if k["kind"] == kind), None)


def kind_options(kinds: list[dict[str, Any]]) -> list[SelectOptionDict]:
    return [SelectOptionDict(value=k["kind"], label=str(k.get("label") or k["kind"])) for k in kinds]


def _fields(kind: dict[str, Any]) -> list[dict[str, Any]]:
    return list(kind.get("fields", []))


def _facts(kind: dict[str, Any]) -> list[dict[str, Any]]:
    return list(kind.get("facts", []))


def _required(kind: dict[str, Any], spec: dict[str, Any]) -> bool:
    """A field in a one_of group is never required alone: the gateway says when the whole group is missing."""
    grouped = {fid for group in kind.get("one_of", []) for fid in group}
    return bool(spec.get("required")) and spec["id"] not in grouped


def _fact_selector(fact: dict[str, Any]) -> Any:
    kind = fact.get("type")
    if kind == "time":
        return TimeSelector()
    if kind == "days":
        return SelectSelector(SelectSelectorConfig(options=DAYS, multiple=True, translation_key="days"))
    if kind == "choice":
        return SelectSelector(SelectSelectorConfig(options=[SelectOptionDict(value=str(o), label=str(o)) for o in fact.get("options", [])]))
    config = NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any")
    if "min" in fact:
        config["min"] = fact["min"]
    if "max" in fact:
        config["max"] = fact["max"]
    if "unit" in fact:
        config["unit_of_measurement"] = fact["unit"]
    return NumberSelector(config)


def form_schema(kind: dict[str, Any]) -> vol.Schema:
    """Name, then each field (and its reversed switch where allowed), then each fact, in served order."""
    schema: dict[Any, Any] = {vol.Required(NAME): TextSelector()}
    if kind["kind"] == _LABELLED:
        schema[vol.Optional(LABEL)] = TextSelector()
    for field in _fields(kind):
        marker = vol.Required if _required(kind, field) else vol.Optional
        domains = list(field.get("domains", []))
        schema[marker(field["id"])] = EntitySelector(EntitySelectorConfig(filter=[EntityFilterSelectorConfig(domain=domains)]))
        if field.get("invert"):
            schema[vol.Optional(field["id"] + INVERT, default=False)] = BooleanSelector()
    for fact in _facts(kind):
        marker = vol.Required if fact.get("required") else vol.Optional
        schema[marker(fact["id"])] = _fact_selector(fact)
    return vol.Schema(schema)


def _fact_value_for_form(fact: dict[str, Any], value: Any) -> Any:
    return str(value) if fact.get("type") == "choice" else value


def suggested_for_new(kind: dict[str, Any]) -> dict[str, Any]:
    """A new device's form: its kind's label as the name and each fact's served default."""
    out: dict[str, Any] = {NAME: str(kind.get("label") or kind["kind"])}
    for fact in _facts(kind):
        if fact.get("default") is not None:
            out[fact["id"]] = _fact_value_for_form(fact, fact["default"])
    return out


def suggested_from_device(
    kind: dict[str, Any], name: str, mapping: dict[str, Any], facts: dict[str, Any], label: str | None = None
) -> dict[str, Any]:
    """An existing device's form: its name (and a load's label), mapped entities (and reversed switches) and facts."""
    out: dict[str, Any] = {NAME: name}
    if kind["kind"] == _LABELLED and label:
        out[LABEL] = label
    for field in _fields(kind):
        m = mapping.get(field["id"])
        if not isinstance(m, dict) or not m.get("entity_id"):
            continue
        out[field["id"]] = m["entity_id"]
        if field.get("invert"):
            out[field["id"] + INVERT] = bool(m.get("invert", False))
    for fact in _facts(kind):
        if facts.get(fact["id"]) is not None:
            out[fact["id"]] = _fact_value_for_form(fact, facts[fact["id"]])
    return out


def _fact_value_for_gateway(fact: dict[str, Any], value: Any) -> Any:
    kind = fact.get("type")
    if kind == "time":
        return str(value)[:5]  # the picker gives HH:MM:SS; the gateway takes HH:MM
    if kind == "days":
        return list(value)
    if kind == "choice":
        return next((o for o in fact.get("options", []) if str(o) == str(value)), value)
    number = float(value)
    return int(number) if number.is_integer() else number


def device_body(kind: dict[str, Any], user_input: dict[str, Any]) -> dict[str, Any]:
    """{name, mapping, facts} for /ha/devices; a field or fact left empty is left out."""
    mapping: dict[str, dict[str, Any]] = {}
    for field in _fields(kind):
        entity_id = user_input.get(field["id"])
        if not entity_id:
            continue
        mapped: dict[str, Any] = {"entity_id": entity_id}
        if field.get("invert"):
            mapped["invert"] = bool(user_input.get(field["id"] + INVERT, False))
        mapping[field["id"]] = mapped
    facts: dict[str, Any] = {}
    for fact in _facts(kind):
        value = user_input.get(fact["id"])
        if value is None or value == "" or value == []:
            continue
        facts[fact["id"]] = _fact_value_for_gateway(fact, value)
    body: dict[str, Any] = {NAME: str(user_input[NAME]).strip(), "mapping": mapping, "facts": facts}
    label = str(user_input.get(LABEL) or "").strip()
    if kind["kind"] == _LABELLED and label:
        body[LABEL] = label
    return body


def field_error(kind: dict[str, Any], detail: str) -> tuple[str, str, str]:
    """(form key, label, reason) for a 422's detail: the field or fact it starts with, else the whole form.

    The reason is the detail less its leading field id, since the label already names the field.
    """
    labels = {spec["id"]: str(spec.get("label") or spec["id"]) for spec in (*_fields(kind), *_facts(kind))}
    labels[NAME] = NAME.capitalize()
    if kind["kind"] == _LABELLED:
        labels[LABEL] = LABEL.capitalize()
    match = _FIRST_WORD.match(detail)
    if match and match.group(1) in labels:
        return match.group(1), labels[match.group(1)], detail[match.end() :].strip() or detail
    return "base", str(kind.get("label") or kind["kind"]), detail
