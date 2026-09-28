"""Mapping form helpers."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.halfhour.mapping import mapping_from_input, suggest, suggested_from_mapping

ROLES = [
    {"id": "house_load_w", "label": "House load", "unit": "W", "required": True, "kinds": ["energy", "power"], "device_classes": ["energy", "power"]},
    {"id": "battery_soc_pct", "label": "SoC", "unit": "%", "required": False, "kinds": ["percent"], "device_classes": ["battery"]},
]
PRESETS = [
    {"name": "Other", "match": {"house_load_w": "sensor.other_load"}},
    {"name": "Victron", "match": {"house_load_w": "sensor.gx_device_ac_loads_on_l1", "battery_soc_pct": "sensor.victron_mqtt_*_battery_soc"}},
]


def test_suggest_uses_the_preset_matching_most_roles():
    ids = ["sensor.gx_device_ac_loads_on_l1", "sensor.victron_mqtt_battery_512_battery_soc", "sensor.other_load"]
    assert suggest(ids, PRESETS) == {
        "house_load_w": "sensor.gx_device_ac_loads_on_l1",
        "battery_soc_pct": "sensor.victron_mqtt_battery_512_battery_soc",
    }


def test_suggest_nothing_when_no_preset_matches():
    assert suggest(["sensor.kitchen_temp"], PRESETS) == {}


def test_suggest_takes_the_first_glob_of_a_list_that_matches():
    presets = [{"name": "Victron", "match": {"house_load_w": ["sensor.*_energy_total", "sensor.*_power"]}}]
    assert suggest(["sensor.house_power"], presets) == {"house_load_w": "sensor.house_power"}
    both = ["sensor.house_power", "sensor.house_energy_total"]
    assert suggest(both, presets) == {"house_load_w": "sensor.house_energy_total"}


@pytest.mark.parametrize(
    ("device_class", "kind"),
    [("energy", "energy"), ("power", "power"), ("battery", "percent"), (None, "power")],
)
async def test_mapping_from_input_sets_kind_from_device_class(hass: HomeAssistant, device_class, kind):
    attrs = {"device_class": device_class} if device_class else {}
    hass.states.async_set("sensor.load", "1", attrs)
    role = ROLES[1] if device_class == "battery" else ROLES[0]
    got = mapping_from_input(hass, [role], {role["id"]: "sensor.load", f"{role['id']}_invert": True})
    assert got == {role["id"]: {"entity_id": "sensor.load", "invert": True, "kind": kind}}


async def test_mapping_from_input_skips_unpicked_roles_and_missing_entities(hass: HomeAssistant):
    got = mapping_from_input(hass, ROLES, {"house_load_w": "sensor.gone"})
    assert got == {"house_load_w": {"entity_id": "sensor.gone", "invert": False, "kind": "power"}}


def test_suggested_from_mapping_prefills_entity_and_invert():
    got = suggested_from_mapping({"house_load_w": {"entity_id": "sensor.a", "invert": True, "kind": "energy"}})
    assert got == {"house_load_w": "sensor.a", "house_load_w_invert": True}


async def test_percent_role_is_percent_without_a_device_class(hass: HomeAssistant):
    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    got = mapping_from_input(hass, ROLES, {"battery_soc_pct": "sensor.soc"})
    assert got == {"battery_soc_pct": {"entity_id": "sensor.soc", "invert": False, "kind": "percent"}}
