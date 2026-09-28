"""Mapping form helpers."""

from custom_components.halfhour.mapping import mapping_from_input, suggest

ROLES = [
    {"id": "house_load_w", "label": "House load", "unit": "W", "required": True, "device_classes": ["power"]},
    {"id": "battery_soc_pct", "label": "SoC", "unit": "%", "required": False, "device_classes": ["battery"]},
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


def test_mapping_from_input_keeps_mapped_roles_with_unit_and_invert():
    got = mapping_from_input(ROLES, {"house_load_w": "sensor.load", "house_load_w_invert": True})
    assert got == {"house_load_w": {"entity_id": "sensor.load", "invert": True, "unit": "W"}}
