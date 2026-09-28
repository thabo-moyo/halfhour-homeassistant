"""Pairing, mapping, options and reauth."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.halfhour.api import DeviceLimitError, PairingError, PairResult, RetryLater
from custom_components.halfhour.const import CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DOMAIN

ROLES = [
    {"id": "house_load_w", "label": "House load", "unit": "W", "required": True, "device_classes": ["power"]},
    {"id": "solar_w", "label": "Solar", "unit": "W", "required": False, "device_classes": ["power"]},
]
CONFIG = {"roles": ROLES, "presets": [{"name": "Victron", "match": {"house_load_w": "sensor.gx_*"}}]}
PAIRED = PairResult("hh_dev_new", "hub-1", "Sam")

CLIENT = "custom_components.halfhour.config_flow.HalfhourClient"
SETUP = "custom_components.halfhour.async_setup_entry"


def client(pair=None, config=None):
    c = AsyncMock()
    c.pair = AsyncMock(side_effect=pair) if isinstance(pair, Exception) else AsyncMock(return_value=pair or PAIRED)
    c.config = AsyncMock(return_value=config or CONFIG)
    return c


async def test_pair_then_map_creates_entry(hass: HomeAssistant):
    hass.states.async_set("sensor.gx_device_ac_loads_on_l1", "400", {"unit_of_measurement": "W", "device_class": "power"})
    with patch(CLIENT, return_value=client()), patch(SETUP, return_value=True):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "user"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "https://hh.test", "code": "abcd-2345"})
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "mapping"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"house_load_w": "sensor.gx_device_ac_loads_on_l1", "house_load_w_invert": False})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["title"] == "Sam"
    assert r["data"] == {CONF_URL: "https://hh.test", CONF_TOKEN: "hh_dev_new", CONF_HUB_ID: "hub-1"}
    assert r["options"][CONF_MAPPING] == {"house_load_w": {"entity_id": "sensor.gx_device_ac_loads_on_l1", "invert": False, "unit": "W"}}
    assert r["options"][CONF_ROLES] == ROLES
    assert r["result"].unique_id == "hub-1"


async def test_bad_code_and_unreachable_show_errors(hass: HomeAssistant):
    for err, key in ((PairingError("bad"), "invalid_code"), (RetryLater(), "cannot_connect")):
        with patch(CLIENT, return_value=client(pair=err)):
            r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
            r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "https://hh.test", "code": "X"})
        assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": key}


async def test_device_limit_on_pairing_shows_error(hass: HomeAssistant):
    with patch(CLIENT, return_value=client(pair=DeviceLimitError("no free slot"))):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "https://hh.test", "code": "X"})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "device_limit"}


async def test_device_limit_on_reauth_shows_error(hass: HomeAssistant):
    entry = paired_entry(hass)
    with patch(CLIENT, return_value=client(pair=DeviceLimitError("no free slot"))):
        r = await entry.start_reauth_flow(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "NEWCODE2"})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "device_limit"}


async def test_same_hub_twice_aborts(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="hub-1", data={CONF_URL: "https://hh.test", CONF_TOKEN: "hh_dev_old", CONF_HUB_ID: "hub-1"})
    entry.add_to_hass(hass)
    with patch(CLIENT, return_value=client()):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "https://hh.test", "code": "X"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"
    assert entry.data[CONF_TOKEN] == "hh_dev_new"


def paired_entry(hass) -> MockConfigEntry:
    e = MockConfigEntry(
        domain=DOMAIN,
        unique_id="hub-1",
        data={CONF_URL: "https://hh.test", CONF_TOKEN: "hh_dev_old", CONF_HUB_ID: "hub-1"},
        options={CONF_MAPPING: {"house_load_w": {"entity_id": "sensor.a", "invert": False, "unit": "W"}}, CONF_ROLES: ROLES},
    )
    e.add_to_hass(hass)
    return e


async def test_reauth_replaces_token_on_the_same_hub(hass: HomeAssistant):
    entry = paired_entry(hass)
    with patch(CLIENT, return_value=client()), patch(SETUP, return_value=True):
        r = await entry.start_reauth_flow(hass)
        assert r["step_id"] == "reauth_confirm"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "NEWCODE2"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "hh_dev_new"


async def test_reauth_after_hub_deleted_moves_entry_to_new_hub(hass: HomeAssistant):
    entry = paired_entry(hass)
    with patch(CLIENT, return_value=client(pair=PairResult("hh_dev_x", "hub-2", "Sam"))), patch(SETUP, return_value=True):
        r = await entry.start_reauth_flow(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "NEWCODE2"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reauth_successful"
    assert entry.unique_id == "hub-2"
    assert entry.data[CONF_HUB_ID] == "hub-2"
    assert entry.data[CONF_TOKEN] == "hh_dev_x"
    assert entry.options[CONF_MAPPING] == {"house_load_w": {"entity_id": "sensor.a", "invert": False, "unit": "W"}}
    assert entry.options[CONF_ROLES] == ROLES


async def test_reauth_onto_hub_owned_by_another_entry_aborts(hass: HomeAssistant):
    entry = paired_entry(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="hub-2",
        data={CONF_URL: "https://hh.test", CONF_TOKEN: "hh_dev_stale", CONF_HUB_ID: "hub-2"},
        options={CONF_MAPPING: {}, CONF_ROLES: ROLES},
    )
    other.add_to_hass(hass)
    with patch(CLIENT, return_value=client(pair=PairResult("hh_dev_x", "hub-2", "Sam"))), patch(SETUP, return_value=True):
        r = await entry.start_reauth_flow(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "NEWCODE2"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"
    assert entry.unique_id == "hub-1"
    assert entry.data[CONF_TOKEN] == "hh_dev_old"
    assert other.data[CONF_TOKEN] == "hh_dev_x"


async def test_options_flow_remaps(hass: HomeAssistant):
    entry = paired_entry(hass)
    with patch(SETUP, return_value=True):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        assert r["step_id"] == "init"
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"house_load_w": "sensor.b", "house_load_w_invert": True, "solar_w": "sensor.pv"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MAPPING]["house_load_w"] == {"entity_id": "sensor.b", "invert": True, "unit": "W"}
    assert entry.options[CONF_MAPPING]["solar_w"]["entity_id"] == "sensor.pv"
