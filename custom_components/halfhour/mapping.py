"""The role → entity mapping form, shared by setup and options."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import BooleanSelector, EntitySelector, EntitySelectorConfig

from .slots import Kind


def _first_hit(entity_ids: list[str], pattern: str | list[str]) -> str | None:
    """The first entity matching the first glob (of an ordered list) that matches anything."""
    for glob in [pattern] if isinstance(pattern, str) else pattern:
        hit = next((e for e in entity_ids if fnmatchcase(e, glob)), None)
        if hit:
            return hit
    return None


def suggest(entity_ids: list[str], presets: list[dict[str, Any]]) -> dict[str, str]:
    """Pre-fill from the preset whose globs match the most roles here."""
    ordered = sorted(entity_ids)
    best: dict[str, str] = {}
    for preset in presets:
        found: dict[str, str] = {}
        for role, pattern in preset["match"].items():
            hit = _first_hit(ordered, pattern)
            if hit:
                found[role] = hit
        if len(found) > len(best):
            best = found
    return best


def mapping_schema(roles: list[dict[str, Any]], suggested: dict[str, Any]) -> vol.Schema:
    """One entity picker per role, plus an invert switch for power roles."""
    fields: dict[Any, Any] = {}
    for role in roles:
        rid = role["id"]
        marker = vol.Required if role["required"] else vol.Optional
        fields[marker(rid, description={"suggested_value": suggested.get(rid)})] = EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class=role["device_classes"])
        )
        if role["unit"] == "W":
            fields[vol.Optional(f"{rid}_invert", default=bool(suggested.get(f"{rid}_invert", False)))] = BooleanSelector()
    return vol.Schema(fields)


def picked(roles: list[dict[str, Any]], user_input: dict[str, Any]) -> dict[str, str]:
    """role id -> the entity chosen for it, for roles given one."""
    return {role["id"]: user_input[role["id"]] for role in roles if user_input.get(role["id"])}


def entity_kind(hass: HomeAssistant, role: dict[str, Any], entity_id: str) -> Kind:
    """How to read an entity's statistics: from its device class, else from the role."""
    state = hass.states.get(entity_id)
    device_class = state.attributes.get("device_class") if state else None
    if device_class == "energy":
        return "energy"
    if device_class == "battery" or role.get("unit") == "%":
        return "percent"
    return "power"


def mapping_from_input(hass: HomeAssistant, roles: list[dict[str, Any]], user_input: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The stored mapping: only roles the user picked an entity for."""
    out: dict[str, dict[str, Any]] = {}
    for role in roles:
        rid = role["id"]
        entity_id = user_input.get(rid)
        if entity_id:
            invert = bool(user_input.get(f"{rid}_invert", False))
            out[rid] = {"entity_id": entity_id, "invert": invert, "kind": entity_kind(hass, role, entity_id)}
    return out


def suggested_from_mapping(mapping: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The form's pre-fill for an existing mapping (options flow)."""
    out: dict[str, Any] = {}
    for rid, m in mapping.items():
        out[rid] = m["entity_id"]
        out[f"{rid}_invert"] = m.get("invert", False)
    return out
