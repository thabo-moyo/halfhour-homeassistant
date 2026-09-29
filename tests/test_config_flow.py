"""Pairing, mapping, options (sensors and devices), reauth and reconfigure."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import BooleanSelector, EntitySelector, NumberSelector, SelectSelector, TimeSelector
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse

from custom_components.halfhour.api import DeviceLimitError, PairingError, PairResult, RetryLater
from custom_components.halfhour.const import CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DOMAIN

# The integration depends on the recorder, so loading its flow sets that up.
pytestmark = pytest.mark.usefixtures("recorder_mock")

ROLES = [
    {"id": "house_load_w", "label": "House load", "unit": "W", "required": True, "device_classes": ["power"]},
    {"id": "solar_w", "label": "Solar", "unit": "W", "required": False, "device_classes": ["power"]},
]
CONFIG = {"roles": ROLES, "presets": [{"name": "Victron", "match": {"house_load_w": "sensor.gx_*"}}]}
PAIRED = PairResult("hh_dev_new", "hub-1", "Sam")

CLIENT = "custom_components.halfhour.config_flow.HalfhourClient"
SETUP = "custom_components.halfhour.async_setup_entry"
HAS_STATS = "custom_components.halfhour.config_flow.has_statistics"


@pytest.fixture(autouse=True)
def has_stats():
    """Every picked entity has statistics unless a test says otherwise."""
    with patch(HAS_STATS, AsyncMock(return_value=True)) as m:
        yield m


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
    assert r["options"][CONF_MAPPING] == {"house_load_w": {"entity_id": "sensor.gx_device_ac_loads_on_l1", "invert": False, "kind": "power"}}
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
        assert r["type"] is FlowResultType.MENU and r["step_id"] == "init"
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "sensors"})
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "sensors"
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"house_load_w": "sensor.b", "house_load_w_invert": True, "solar_w": "sensor.pv"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MAPPING]["house_load_w"] == {"entity_id": "sensor.b", "invert": True, "kind": "power"}
    assert entry.options[CONF_MAPPING]["solar_w"]["entity_id"] == "sensor.pv"


async def test_mapping_refuses_an_entity_without_statistics(hass: HomeAssistant, has_stats: AsyncMock):
    hass.states.async_set("sensor.load", "400", {"device_class": "power"})
    hass.states.async_set("sensor.pv_total", "12.5", {"device_class": "energy"})
    has_stats.side_effect = lambda _hass, eid: eid != "sensor.load"
    with patch(CLIENT, return_value=client()), patch(SETUP, return_value=True):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "https://hh.test", "code": "X"})
        pick = {"house_load_w": "sensor.load", "solar_w": "sensor.pv_total"}
        r = await hass.config_entries.flow.async_configure(r["flow_id"], pick)
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "mapping"
        assert r["errors"] == {"house_load_w": "no_statistics"}
        assert not hass.config_entries.async_entries(DOMAIN)
        has_stats.side_effect = None
        r = await hass.config_entries.flow.async_configure(r["flow_id"], pick)
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["options"][CONF_MAPPING] == {
        "house_load_w": {"entity_id": "sensor.load", "invert": False, "kind": "power"},
        "solar_w": {"entity_id": "sensor.pv_total", "invert": False, "kind": "energy"},
    }


async def test_mapping_aborts_when_config_cannot_be_read(hass: HomeAssistant):
    c = client()
    c.config = AsyncMock(side_effect=RetryLater())
    with patch(CLIENT, return_value=c):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "https://hh.test", "code": "X"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "cannot_connect"


async def test_options_flow_refuses_an_entity_without_statistics(hass: HomeAssistant, has_stats: AsyncMock):
    entry = paired_entry(hass)
    has_stats.return_value = False
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "sensors"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"house_load_w": "sensor.b", "solar_w": "sensor.pv"})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "sensors"
    assert r["errors"] == {"house_load_w": "no_statistics", "solar_w": "no_statistics"}
    assert entry.options[CONF_MAPPING]["house_load_w"]["entity_id"] == "sensor.a"


@pytest.mark.parametrize(("err", "key"), [(PairingError("bad"), "invalid_code"), (RetryLater(), "cannot_connect")])
async def test_reauth_errors_show_on_the_form(hass: HomeAssistant, err, key):
    entry = paired_entry(hass)
    with patch(CLIENT, return_value=client(pair=err)):
        r = await entry.start_reauth_flow(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "NEWCODE2"})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": key}


async def test_reconfigure_shows_url_and_code(hass: HomeAssistant):
    entry = paired_entry(hass)
    r = await entry.start_reconfigure_flow(hass)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "reconfigure"
    assert set(r["data_schema"].schema) == {CONF_URL, "code"}
    url_key = next(k for k in r["data_schema"].schema if k == CONF_URL)
    assert url_key.default() == "https://hh.test"


async def test_reconfigure_pairs_at_a_new_url_keeping_the_entry(hass: HomeAssistant):
    entry = paired_entry(hass)
    entry_id = entry.entry_id
    c = client(pair=PairResult("hh_dev_moved", "hub-9", "Sam"))
    with patch(CLIENT, return_value=c) as cls, patch(SETUP, return_value=True):
        r = await entry.start_reconfigure_flow(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "https://new.hh.test", "code": " NEWCODE2 "})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reconfigure_successful"
    assert cls.call_args.args[1] == "https://new.hh.test"
    assert c.pair.call_args.args[0] == "NEWCODE2"
    assert entry.entry_id == entry_id and entry.unique_id == "hub-9" and entry.title == "Sam"
    assert entry.data == {CONF_URL: "https://new.hh.test", CONF_TOKEN: "hh_dev_moved", CONF_HUB_ID: "hub-9"}
    assert entry.options[CONF_MAPPING]["house_load_w"]["entity_id"] == "sensor.a"


@pytest.mark.parametrize(
    ("err", "key"),
    [(PairingError("bad"), "invalid_code"), (DeviceLimitError("full"), "device_limit"), (RetryLater(), "cannot_connect")],
)
async def test_reconfigure_errors_show_on_the_form(hass: HomeAssistant, err, key):
    entry = paired_entry(hass)
    with patch(CLIENT, return_value=client(pair=err)):
        r = await entry.start_reconfigure_flow(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "https://new.hh.test", "code": "X"})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "reconfigure" and r["errors"] == {"base": key}
    assert entry.data[CONF_URL] == "https://hh.test"


async def test_reconfigure_onto_hub_owned_by_another_entry_aborts(hass: HomeAssistant):
    entry = paired_entry(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="hub-2",
        data={CONF_URL: "https://new.hh.test", CONF_TOKEN: "hh_dev_stale", CONF_HUB_ID: "hub-2"},
        options={CONF_MAPPING: {}, CONF_ROLES: ROLES},
    )
    other.add_to_hass(hass)
    with patch(CLIENT, return_value=client(pair=PairResult("hh_dev_x", "hub-2", "Sam"))), patch(SETUP, return_value=True):
        r = await entry.start_reconfigure_flow(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "https://new.hh.test", "code": "X"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"
    assert entry.unique_id == "hub-1" and entry.data[CONF_URL] == "https://hh.test"
    # /ha/pair rotated the other entry's token; it gets the new one.
    assert other.data[CONF_TOKEN] == "hh_dev_x"


async def test_reconfigure_to_a_new_hub_backfills_it_from_the_start(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    entry = paired_entry(hass)
    cursors = {"house_load_w": {"entity_id": "sensor.a", "cursor": "2026-09-28T11:30:00+00:00"}}
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.cursors").async_save({"hub_id": "hub-1", "cursors": cursors})
    aioclient_mock.get("https://new.hh.test/api/v1/ha/config", json=CONFIG)
    aioclient_mock.put("https://new.hh.test/api/v1/ha/inventory", status=204)
    with (
        patch(CLIENT, return_value=client(pair=PairResult("hh_dev_moved", "hub-9", "Sam"))),
        patch("custom_components.halfhour.sync.HalfhourSync.async_start"),
    ):
        r = await entry.start_reconfigure_flow(hass)
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "https://new.hh.test", "code": "X"})
        await hass.async_block_till_done()
    assert r["reason"] == "reconfigure_successful"
    assert entry.runtime_data.sync.cursors == {}


# -- options: devices behind Home Assistant ---------------------------------------------

URL = "https://hh.test/api/v1"
B = "3f2a0000-0000-4000-8000-00000000000b"
BATTERY = {
    "id": B,
    "kind": "battery",
    "name": "Home battery",
    "mapping": {"soc": {"entity_id": "sensor.bat_soc"}, "power": {"entity_id": "sensor.bat_power", "invert": True}, "grid_setpoint": {"entity_id": "number.setpoint"}},
    "facts": {"capacity_kwh": 10, "max_charge_w": 3000, "max_discharge_w": 3000},
}
# What GET /device-types serves as hub_kinds (the gateway's schema; the Integration holds no copy).
HUB_KINDS = [
    {
        "kind": "battery",
        "label": "Home battery",
        "fields": [
            {"id": "soc", "label": "Charge level", "required": True, "domains": ["sensor"], "device_classes": ["battery"], "units": ["%"], "role": "read"},
            {"id": "power", "label": "Power (+ while charging)", "required": True, "domains": ["sensor"], "device_classes": ["power"], "units": ["W", "kW"], "invert": True, "role": "read"},
            {"id": "grid_setpoint", "label": "Grid setpoint", "required": True, "domains": ["number"], "units": ["W"], "role": "control"},
            {"id": "floor", "label": "Reserve floor", "required": False, "domains": ["number", "sensor"], "units": ["%"], "role": "control"},
        ],
        "facts": [
            {"id": "capacity_kwh", "label": "Usable capacity", "required": True, "type": "number", "min": 0.5, "max": 200, "unit": "kWh"},
            {"id": "max_charge_w", "label": "Max charge power", "required": True, "type": "number", "min": 100, "max": 50000, "unit": "W"},
            {"id": "max_discharge_w", "label": "Max discharge power", "required": True, "type": "number", "min": 100, "max": 50000, "unit": "W"},
            {"id": "floor_pct", "label": "Reserve floor (no floor entity)", "required": False, "type": "number", "min": 0, "max": 100, "unit": "%"},
        ],
    },
    {
        "kind": "solar",
        "label": "Solar",
        "fields": [
            {"id": "power", "label": "Power", "required": False, "domains": ["sensor"], "device_classes": ["power"], "units": ["W", "kW"], "role": "read"},
            {"id": "energy", "label": "Energy total", "required": False, "domains": ["sensor"], "device_classes": ["energy"], "units": ["kWh"], "state_class": "total_increasing", "role": "read"},
        ],
        "facts": [{"id": "kwp", "label": "Panel capacity", "required": False, "type": "number", "min": 0.1, "max": 100, "unit": "kWp"}],
        "one_of": [["power", "energy"]],
    },
    {
        "kind": "ev-charger",
        "label": "EV charger",
        "fields": [
            {"id": "plugged_in", "label": "Plugged in", "required": True, "domains": ["binary_sensor"], "role": "read"},
            {"id": "charge_switch", "label": "Charge switch", "required": True, "domains": ["switch"], "role": "control"},
        ],
        "facts": [
            {"id": "phases", "label": "Phases", "required": False, "type": "choice", "options": [1, 3], "default": 1},
            {"id": "voltage", "label": "Supply voltage", "required": False, "type": "number", "default": 230, "unit": "V"},
        ],
    },
    {
        "kind": "load",
        "label": "Load",
        "fields": [
            {"id": "power", "label": "Power", "required": False, "domains": ["sensor"], "device_classes": ["power"], "units": ["W", "kW"], "role": "read"},
            {"id": "switch", "label": "Switch", "required": True, "domains": ["switch", "input_boolean"], "role": "control"},
        ],
        "facts": [
            {"id": "rated_w", "label": "Rated power", "required": True, "type": "number", "min": 10, "max": 15000, "unit": "W"},
            {"id": "run_minutes", "label": "Run time", "required": True, "type": "number", "min": 5, "max": 1440, "unit": "min"},
            {"id": "earliest", "label": "Earliest start", "required": False, "type": "time"},
            {"id": "days", "label": "Days", "required": False, "type": "days", "default": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        ],
    },
]
BATTERY_INPUT = {
    "name": "Garage battery",
    "soc": "sensor.bat_soc",
    "power": "sensor.bat_power",
    "power_invert": True,
    "grid_setpoint": "number.setpoint",
    "capacity_kwh": 9.5,
    "max_charge_w": 3000,
    "max_discharge_w": 3000,
}


def device_list(revision: int, *devices: dict) -> bytes:
    return json.dumps({"v": 1, "revision": revision, "devices": list(devices)}).encode()


@pytest.fixture
def gateway(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    aioclient_mock.get(f"{URL}/device-types", json={"items": [], "hub_kinds": HUB_KINDS})
    return aioclient_mock


def sent(mock: AiohttpClientMocker, method: str, path: str) -> list:
    return [data for m, url, data, _ in mock.mock_calls if m.lower() == method and str(url) == URL + path]


async def loaded_entry(hass: HomeAssistant, mock: AiohttpClientMocker, *devices: dict, revision: int = 3) -> MockConfigEntry:
    """A set-up entry holding a device list, as after a retained list arrived."""
    mock.get(f"{URL}/ha/config", json=CONFIG)
    mock.put(f"{URL}/ha/inventory", status=204)
    entry = paired_entry(hass)
    with patch("custom_components.halfhour.sync.HalfhourSync.async_start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entry.runtime_data.devices.offer(device_list(revision, *devices))
    return entry


async def open_menu(hass: HomeAssistant, entry: MockConfigEntry, choice: str) -> dict:
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": choice})


def field(result: dict, key: str):
    """The schema key and selector for one form field."""
    marker = next(k for k in result["data_schema"].schema if k == key)
    return marker, result["data_schema"].schema[marker]


async def test_options_menu_without_devices(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = paired_entry(hass)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] is FlowResultType.MENU and r["menu_options"] == ["sensors", "add_device"]


async def test_options_menu_with_devices(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = await loaded_entry(hass, gateway, BATTERY)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] is FlowResultType.MENU
    assert r["menu_options"] == ["sensors", "add_device", "edit_device", "remove_device"]


async def test_add_device_offers_the_served_kinds(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = paired_entry(hass)
    r = await open_menu(hass, entry, "add_device")
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "add_device"
    _, selector = field(r, "kind")
    assert [(o["value"], o["label"]) for o in selector.config["options"]] == [
        ("battery", "Home battery"),
        ("solar", "Solar"),
        ("ev-charger", "EV charger"),
        ("load", "Load"),
    ]


async def test_add_device_aborts_when_the_kinds_cannot_be_read(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(f"{URL}/device-types", status=503)
    entry = paired_entry(hass)
    r = await open_menu(hass, entry, "add_device")
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "cannot_connect"


async def test_add_device_aborts_against_a_gateway_too_old_for_devices(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(f"{URL}/device-types", json={"items": []})  # served before devices behind a hub existed
    entry = paired_entry(hass)
    r = await open_menu(hass, entry, "add_device")
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "gateway_too_old"


async def test_the_form_has_the_right_selectors_per_kind(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = paired_entry(hass)

    async def form(kind: str) -> dict:
        r = await open_menu(hass, entry, "add_device")
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": kind})
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "add_details"
        return r

    r = await form("battery")
    assert r["description_placeholders"]["kind"] == "Home battery"
    assert "label" not in [str(k) for k in r["data_schema"].schema]  # only a load has one
    keys = [str(k) for k in r["data_schema"].schema]
    assert keys == ["name", "soc", "power", "power_invert", "grid_setpoint", "floor", "capacity_kwh", "max_charge_w", "max_discharge_w", "floor_pct"]
    marker, sel = field(r, "power")
    assert isinstance(sel, EntitySelector) and isinstance(marker, vol.Required)
    # Domain only: the gateway also takes an entity with no device class when its unit fits.
    assert sel.config["filter"] == [{"domain": ["sensor"]}]
    assert isinstance(field(r, "power_invert")[1], BooleanSelector)
    assert "soc_invert" not in keys
    marker, sel = field(r, "floor")
    assert isinstance(marker, vol.Optional) and sel.config["filter"] == [{"domain": ["number", "sensor"]}]
    marker, sel = field(r, "capacity_kwh")
    assert isinstance(sel, NumberSelector) and isinstance(marker, vol.Required)
    assert (sel.config["min"], sel.config["max"], sel.config["unit_of_measurement"]) == (0.5, 200, "kWh")
    assert isinstance(field(r, "floor_pct")[0], vol.Optional)

    r = await form("solar")
    # one_of: neither alone is required; the gateway says when both are missing.
    assert isinstance(field(r, "power")[0], vol.Optional) and isinstance(field(r, "energy")[0], vol.Optional)
    assert "power_invert" not in [str(k) for k in r["data_schema"].schema]

    r = await form("ev-charger")
    marker, sel = field(r, "phases")
    assert isinstance(sel, SelectSelector) and [o["value"] for o in sel.config["options"]] == ["1", "3"]
    assert marker.description == {"suggested_value": "1"}
    assert field(r, "voltage")[0].description == {"suggested_value": 230}
    assert field(r, "plugged_in")[1].config["filter"] == [{"domain": ["binary_sensor"]}]

    r = await form("load")
    assert [str(k) for k in r["data_schema"].schema][:2] == ["name", "label"]
    assert isinstance(field(r, "label")[0], vol.Optional)
    assert field(r, "switch")[1].config["filter"] == [{"domain": ["switch", "input_boolean"]}]
    assert isinstance(field(r, "earliest")[1], TimeSelector)
    marker, sel = field(r, "days")
    assert isinstance(sel, SelectSelector) and sel.config["multiple"] is True
    assert [o for o in sel.config["options"]] == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert marker.description == {"suggested_value": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}


async def test_add_flow_creates_a_device(hass: HomeAssistant, gateway: AiohttpClientMocker):
    gateway.post(f"{URL}/ha/devices", status=201, json={**BATTERY, "name": "Garage battery", "revision": 4})
    entry = paired_entry(hass)
    r = await open_menu(hass, entry, "add_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "battery"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], BATTERY_INPUT)
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "device_added"
    assert r["description_placeholders"] == {"name": "Garage battery"}
    assert sent(gateway, "post", "/ha/devices") == [
        {
            "kind": "battery",
            "name": "Garage battery",
            "mapping": {
                "soc": {"entity_id": "sensor.bat_soc"},
                "power": {"entity_id": "sensor.bat_power", "invert": True},
                "grid_setpoint": {"entity_id": "number.setpoint"},
            },
            "facts": {"capacity_kwh": 9.5, "max_charge_w": 3000, "max_discharge_w": 3000},
        }
    ]
    assert entry.options[CONF_MAPPING]["house_load_w"]["entity_id"] == "sensor.a"  # untouched


async def test_add_flow_sends_times_choices_and_days_as_the_gateway_takes_them(hass: HomeAssistant, gateway: AiohttpClientMocker):
    gateway.post(f"{URL}/ha/devices", status=201, json={"id": B, "kind": "load", "name": "Dishwasher", "revision": 4})
    entry = paired_entry(hass)
    r = await open_menu(hass, entry, "add_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "load"})
    user = {"name": "Kitchen dishwasher", "label": " Dishwasher ", "switch": "switch.dw", "rated_w": 1800.0, "run_minutes": 90, "earliest": "22:30:00", "days": ["sat", "sun"]}
    r = await hass.config_entries.options.async_configure(r["flow_id"], user)
    assert r["type"] is FlowResultType.ABORT
    body = sent(gateway, "post", "/ha/devices")[0]
    assert (body["name"], body["label"]) == ("Kitchen dishwasher", "Dishwasher")
    assert body["facts"] == {"rated_w": 1800, "run_minutes": 90, "earliest": "22:30", "days": ["sat", "sun"]}

    gateway.clear_requests()
    gateway.get(f"{URL}/device-types", json={"items": [], "hub_kinds": HUB_KINDS})
    gateway.post(f"{URL}/ha/devices", status=201, json={"id": B, "kind": "ev-charger", "name": "Car", "revision": 5})
    r = await open_menu(hass, entry, "add_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "ev-charger"})
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"name": "Car", "plugged_in": "binary_sensor.car", "charge_switch": "switch.car", "phases": "3"}
    )
    assert r["type"] is FlowResultType.ABORT
    body = sent(gateway, "post", "/ha/devices")[0]
    assert body["facts"] == {"phases": 3} and "label" not in body


async def test_a_422_names_the_field(hass: HomeAssistant, gateway: AiohttpClientMocker):
    gateway.post(f"{URL}/ha/devices", status=422, json={"detail": "power: sensor.bat_power must be device class power"})
    entry = paired_entry(hass)
    r = await open_menu(hass, entry, "add_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "battery"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], BATTERY_INPUT)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "add_details"
    assert r["errors"] == {"power": "invalid_mapping"}
    assert r["description_placeholders"]["field"] == "Power (+ while charging)"
    # The label names the field, so the gateway's leading id is dropped from the reason.
    assert r["description_placeholders"]["detail"] == "sensor.bat_power must be device class power"
    # What was typed stays on the form.
    assert field(r, "name")[0].description == {"suggested_value": "Garage battery"}


@pytest.mark.parametrize(
    ("detail", "errors", "placeholder"),
    [
        ("capacity_kwh must be at least 0.5", {"capacity_kwh": "invalid_mapping"}, ("Usable capacity", "must be at least 0.5")),
        ("one of power, energy is required", {"base": "invalid_mapping"}, ("Home battery", "one of power, energy is required")),
    ],
)
async def test_a_422_without_a_field_prefix(hass: HomeAssistant, gateway: AiohttpClientMocker, detail, errors, placeholder):
    gateway.post(f"{URL}/ha/devices", status=422, json={"detail": detail})
    entry = paired_entry(hass)
    r = await open_menu(hass, entry, "add_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "battery"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], BATTERY_INPUT)
    assert r["errors"] == errors
    assert (r["description_placeholders"]["field"], r["description_placeholders"]["detail"]) == placeholder


@pytest.mark.parametrize(
    ("status", "body", "key"),
    [(409, {"detail": "Your plan allows 3 devices."}, "device_limit"), (503, {"detail": "busy"}, "cannot_connect")],
)
async def test_plan_limit_and_outage_show_on_the_form(hass: HomeAssistant, gateway: AiohttpClientMocker, status, body, key):
    gateway.post(f"{URL}/ha/devices", status=status, json=body)
    entry = paired_entry(hass)
    r = await open_menu(hass, entry, "add_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "battery"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], BATTERY_INPUT)
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": key}


async def test_add_waits_for_the_new_device_list(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = await loaded_entry(hass, gateway)
    created = {**BATTERY, "revision": 4}

    async def create(method, url, data):
        # The retained list arrives a moment after the gateway answers.
        hass.loop.call_later(0.05, entry.runtime_data.devices.offer, device_list(4, BATTERY))
        return AiohttpClientMockResponse(method, url, status=201, json=created)

    gateway.post(f"{URL}/ha/devices", side_effect=create)
    r = await open_menu(hass, entry, "add_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "battery"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], BATTERY_INPUT)
    assert r["reason"] == "device_added"
    assert entry.runtime_data.devices.revision == 4


async def test_add_finishes_anyway_when_the_list_never_comes(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = await loaded_entry(hass, gateway)
    gateway.post(f"{URL}/ha/devices", status=201, json={**BATTERY, "revision": 4})
    with patch("custom_components.halfhour.config_flow.LIST_WAIT", 0.05):
        r = await open_menu(hass, entry, "add_device")
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "battery"})
        r = await hass.config_entries.options.async_configure(r["flow_id"], BATTERY_INPUT)
    assert r["reason"] == "device_added"
    assert entry.runtime_data.devices.revision == 3


async def test_edit_shows_the_current_device_and_sends_its_revision(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = await loaded_entry(hass, gateway, BATTERY)
    gateway.put(f"{URL}/ha/devices/{B}", json={**BATTERY, "name": "Garage battery", "revision": 3})
    r = await open_menu(hass, entry, "edit_device")
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "edit_device"
    assert [(o["value"], o["label"]) for o in field(r, "device")[1].config["options"]] == [(B, "Home battery")]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": B})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "edit_details"
    assert r["description_placeholders"]["name"] == "Home battery"
    assert field(r, "name")[0].description == {"suggested_value": "Home battery"}
    assert field(r, "power")[0].description == {"suggested_value": "sensor.bat_power"}
    assert field(r, "power_invert")[0].description == {"suggested_value": True}
    assert field(r, "capacity_kwh")[0].description == {"suggested_value": 10}
    r = await hass.config_entries.options.async_configure(r["flow_id"], {**BATTERY_INPUT, "power_invert": False})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "device_updated"
    (body,) = sent(gateway, "put", f"/ha/devices/{B}")
    assert body["revision"] == 3 and body["name"] == "Garage battery"
    assert body["mapping"]["power"] == {"entity_id": "sensor.bat_power", "invert": False}
    assert "kind" not in body


async def test_edit_with_a_stale_revision_reshows_the_latest(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = await loaded_entry(hass, gateway, BATTERY)
    latest = {**BATTERY, "name": "Renamed on the web", "facts": {**BATTERY["facts"], "capacity_kwh": 13.5}, "revision": 5}
    answers = [
        AiohttpClientMockResponse("put", URL, status=409, json={"detail": "This device changed since you loaded it.", "device": latest}),
        AiohttpClientMockResponse("put", URL, status=200, json={**latest, "revision": 6}),
    ]

    async def put(method, url, data):
        return answers.pop(0)

    gateway.put(f"{URL}/ha/devices/{B}", side_effect=put)
    r = await open_menu(hass, entry, "edit_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": B})
    r = await hass.config_entries.options.async_configure(r["flow_id"], BATTERY_INPUT)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "edit_details"
    assert r["errors"] == {"base": "stale_revision"}
    assert field(r, "name")[0].description == {"suggested_value": "Renamed on the web"}
    assert field(r, "capacity_kwh")[0].description == {"suggested_value": 13.5}
    with patch("custom_components.halfhour.config_flow.LIST_WAIT", 0.05):
        r = await hass.config_entries.options.async_configure(r["flow_id"], {**BATTERY_INPUT, "name": "Renamed on the web"})
    assert r["reason"] == "device_updated"
    assert [b["revision"] for b in sent(gateway, "put", f"/ha/devices/{B}")] == [3, 5]


async def test_edit_of_a_device_deleted_elsewhere_aborts(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = await loaded_entry(hass, gateway, BATTERY)
    gateway.put(f"{URL}/ha/devices/{B}", status=404, json={"detail": "no such device"})
    r = await open_menu(hass, entry, "edit_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": B})
    r = await hass.config_entries.options.async_configure(r["flow_id"], BATTERY_INPUT)
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "not_found"


async def test_edit_of_a_kind_the_gateway_no_longer_serves_aborts(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    entry = await loaded_entry(hass, aioclient_mock, BATTERY)
    aioclient_mock.get(f"{URL}/device-types", json={"items": [], "hub_kinds": [k for k in HUB_KINDS if k["kind"] != "battery"]})
    r = await open_menu(hass, entry, "edit_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": B})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "unknown_kind"


async def test_remove_deletes_the_device(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = await loaded_entry(hass, gateway, BATTERY)

    async def delete(method, url, data):
        hass.loop.call_later(0.05, entry.runtime_data.devices.offer, device_list(4))
        return AiohttpClientMockResponse(method, url, status=204)

    gateway.delete(f"{URL}/ha/devices/{B}", side_effect=delete)
    r = await open_menu(hass, entry, "remove_device")
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "remove_device"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": B})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "device_removed"
    assert r["description_placeholders"] == {"name": "Home battery"}
    assert len(sent(gateway, "delete", f"/ha/devices/{B}")) == 1
    assert entry.runtime_data.devices.device(B) is None


@pytest.mark.parametrize(("status", "outcome"), [(404, ("abort", "not_found")), (503, ("form", "cannot_connect"))])
async def test_remove_errors(hass: HomeAssistant, gateway: AiohttpClientMocker, status, outcome):
    entry = await loaded_entry(hass, gateway, BATTERY)
    gateway.delete(f"{URL}/ha/devices/{B}", status=status, json={"detail": "x"})
    r = await open_menu(hass, entry, "remove_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": B})
    if outcome[0] == "abort":
        assert r["type"] is FlowResultType.ABORT and r["reason"] == outcome[1]
    else:
        assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": outcome[1]}


@pytest.mark.parametrize(
    ("status", "body", "errors"),
    [(422, {"detail": "soc: sensor.x must report %"}, {"soc": "invalid_mapping"}), (503, {"detail": "busy"}, {"base": "cannot_connect"})],
)
async def test_edit_errors_show_on_the_form(hass: HomeAssistant, gateway: AiohttpClientMocker, status, body, errors):
    entry = await loaded_entry(hass, gateway, BATTERY)
    gateway.put(f"{URL}/ha/devices/{B}", status=status, json=body)
    r = await open_menu(hass, entry, "edit_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": B})
    r = await hass.config_entries.options.async_configure(r["flow_id"], BATTERY_INPUT)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "edit_details" and r["errors"] == errors


async def test_edit_aborts_when_the_kinds_cannot_be_read(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    entry = await loaded_entry(hass, aioclient_mock, BATTERY)
    aioclient_mock.get(f"{URL}/device-types", json={"items": []})  # no hub_kinds
    r = await open_menu(hass, entry, "edit_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": B})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "gateway_too_old"


async def test_edit_aborts_when_the_gateway_cannot_be_reached(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    entry = await loaded_entry(hass, aioclient_mock, BATTERY)
    aioclient_mock.get(f"{URL}/device-types", status=503)
    r = await open_menu(hass, entry, "edit_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": B})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "cannot_connect"


async def test_edit_of_a_device_gone_from_the_list_since_the_menu_aborts(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = await loaded_entry(hass, gateway, BATTERY)
    r = await open_menu(hass, entry, "edit_device")
    entry.runtime_data.devices.offer(device_list(4))
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": B})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "not_found"


async def test_a_write_answer_without_a_revision_does_not_wait(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = await loaded_entry(hass, gateway)
    gateway.post(f"{URL}/ha/devices", status=201, json={"id": B, "name": "Garage battery"})
    with patch("custom_components.halfhour.config_flow.LIST_WAIT", 60):
        r = await open_menu(hass, entry, "add_device")
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "battery"})
        r = await asyncio.wait_for(hass.config_entries.options.async_configure(r["flow_id"], BATTERY_INPUT), 1)
    assert r["reason"] == "device_added"


L = "3f2a0000-0000-4000-8000-00000000000c"
DISHWASHER = {"id": L, "kind": "load", "name": "Kitchen dishwasher", "label": "Dishwasher", "mapping": {"switch": {"entity_id": "switch.dw"}}, "facts": {"rated_w": 1800, "run_minutes": 90}}


async def test_edit_a_loads_label(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = await loaded_entry(hass, gateway, DISHWASHER)
    gateway.put(f"{URL}/ha/devices/{L}", json={**DISHWASHER, "label": "Dish washer", "revision": 4})
    r = await open_menu(hass, entry, "edit_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": L})
    assert field(r, "label")[0].description == {"suggested_value": "Dishwasher"}
    with patch("custom_components.halfhour.config_flow.LIST_WAIT", 0.05):
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {"name": "Kitchen dishwasher", "label": "Dish washer", "switch": "switch.dw", "rated_w": 1800, "run_minutes": 90}
        )
    assert r["reason"] == "device_updated"
    assert sent(gateway, "put", f"/ha/devices/{L}")[0]["label"] == "Dish washer"


async def test_a_stale_edit_of_a_load_shows_the_latest_label(hass: HomeAssistant, gateway: AiohttpClientMocker):
    entry = await loaded_entry(hass, gateway, DISHWASHER)
    latest = {**DISHWASHER, "label": "Renamed", "revision": 5}
    gateway.put(f"{URL}/ha/devices/{L}", status=409, json={"detail": "changed", "device": latest})
    r = await open_menu(hass, entry, "edit_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"device": L})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"name": "Kitchen dishwasher", "switch": "switch.dw", "rated_w": 1800, "run_minutes": 90})
    assert r["errors"] == {"base": "stale_revision"}
    assert field(r, "label")[0].description == {"suggested_value": "Renamed"}


async def test_a_422_on_the_label(hass: HomeAssistant, gateway: AiohttpClientMocker):
    gateway.post(f"{URL}/ha/devices", status=422, json={"detail": "label must be 1-60 characters"})
    entry = paired_entry(hass)
    r = await open_menu(hass, entry, "add_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "load"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"name": "Dishwasher", "label": "x" * 61, "switch": "switch.dw", "rated_w": 1800, "run_minutes": 90})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"label": "invalid_mapping"}
    assert (r["description_placeholders"]["field"], r["description_placeholders"]["detail"]) == ("Label", "must be 1-60 characters")


async def test_a_kind_withdrawn_since_the_form_was_shown_is_an_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    served = [{"items": [], "hub_kinds": HUB_KINDS}, {"items": [], "hub_kinds": [k for k in HUB_KINDS if k["kind"] != "load"]}]

    async def device_types(method, url, data):
        return AiohttpClientMockResponse(method, url, json=served.pop(0))

    aioclient_mock.get(f"{URL}/device-types", side_effect=device_types)
    entry = paired_entry(hass)
    r = await open_menu(hass, entry, "add_device")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "load"})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "add_device"
    assert r["errors"] == {"kind": "kind_unavailable"}
