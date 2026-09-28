"""The wire contract, pinned by the optimiser's own bytes.

tests/fixtures/plan_golden.json is written by the optimiser's
`TestPayloadGolden` (emhass-optimiser/internal/planpub) from a fixed run in
its internal units: SOC 0.65 as a fraction, battery -3000 W (EMHASS: charging).
On the wire that must arrive as soc 65 (%) and battery_w +3000 (+ charging).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homeassistant.components.recorder import Recorder
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.halfhour.plan import parse_plan

from .fakes import FakeTransport
from .test_plan_entities import BATTERY, GRID, SOC, config, connect, state, transport  # noqa: F401 - transport is a fixture
from .test_plan_entities import setup as setup_entry

GOLDEN = Path(__file__).parent / "fixtures" / "plan_golden.json"


def test_the_golden_payload_parses_in_wire_units() -> None:
    plan = parse_plan(GOLDEN.read_bytes())
    assert plan.id == "golden-1"
    assert plan.account == "default"
    charging, discharging, unknown = plan.slots
    assert charging.soc == 65
    assert charging.battery_w == 3000
    assert charging.grid_w == 3400
    assert charging.import_p == 7
    assert discharging.soc == 50
    assert discharging.battery_w == -2000
    assert discharging.grid_w == -1650
    assert (unknown.soc, unknown.battery_w, unknown.grid_w, unknown.load_w) == (None, None, None, None)


async def test_the_golden_payload_shows_percent_and_plus_charging(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, transport: FakeTransport, freezer: Any
) -> None:
    now = datetime(2026, 9, 28, 0, 10, tzinfo=UTC)
    freezer.move_to(now)
    body = config()
    body["mqtt"]["account"] = "default"
    await setup_entry(hass, aioclient_mock, body)
    await connect(hass, transport)
    transport.on_message("accounts/default/plan", GOLDEN.read_bytes())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert float(state(hass, SOC)) == 65
    assert float(state(hass, BATTERY)) == 3000  # charging, so positive
    assert float(state(hass, GRID)) == 3400  # importing, so positive

    later = datetime(2026, 9, 28, 1, 10, tzinfo=UTC)
    freezer.move_to(later)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, SOC) == STATE_UNKNOWN
    assert state(hass, BATTERY) == STATE_UNKNOWN
