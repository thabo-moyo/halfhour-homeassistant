"""The role → entity mapping form, shared by setup and options."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import BooleanSelector, EntitySelector, EntitySelectorConfig


def suggest(entity_ids: list[str], presets: list[dict[str, Any]]) -> dict[str, str]:
    """Pre-fill from the preset whose globs match the most roles here."""
    best: dict[str, str] = {}
    for preset in presets:
        found: dict[str, str] = {}
        for role, pattern in preset["match"].items():
            hit = next((e for e in sorted(entity_ids) if fnmatchcase(e, pattern)), None)
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


def mapping_from_input(roles: list[dict[str, Any]], user_input: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The stored mapping: only roles the user picked an entity for."""
    out: dict[str, dict[str, Any]] = {}
    for role in roles:
        entity_id = user_input.get(role["id"])
        if entity_id:
            out[role["id"]] = {"entity_id": entity_id, "invert": bool(user_input.get(f"{role['id']}_invert", False)), "unit": role["unit"]}
    return out


def suggested_from_mapping(mapping: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The form's pre-fill for an existing mapping (options flow)."""
    out: dict[str, Any] = {}
    for rid, m in mapping.items():
        out[rid] = m["entity_id"]
        out[f"{rid}_invert"] = m.get("invert", False)
    return out
