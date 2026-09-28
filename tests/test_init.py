"""Setting up, unloading and removing an entry."""

from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.halfhour.const import CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DOMAIN

ROLES = [{"id": "house_load_w", "label": "House load", "unit": "W", "kind": "power", "required": True, "device_classes": ["power"]}]
CONFIG = {"roles": ROLES, "presets": []}
LOAD = {"house_load_w": {"entity_id": "sensor.load", "invert": False, "unit": "W"}}


def make_entry(hass: HomeAssistant, roles: list | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sam",
        unique_id="hub-1",
        data={CONF_URL: "https://hh.test", CONF_TOKEN: "t", CONF_HUB_ID: "hub-1"},
        options={CONF_MAPPING: LOAD, CONF_ROLES: ROLES if roles is None else roles},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def server(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    aioclient_mock.get("https://hh.test/api/v1/ha/config", json=CONFIG)
    aioclient_mock.post("https://hh.test/api/v1/ha/telemetry", status=202, json={"accepted": 0})
    return aioclient_mock


async def test_setup_creates_diagnostic_entities_and_unloads(recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker) -> None:
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED

    for entity_id in ("sensor.halfhour_last_upload", "sensor.halfhour_synced_up_to", "binary_sensor.halfhour_connected"):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get("sensor.halfhour_queued_samples") is None

    sync = entry.runtime_data.sync
    with patch.object(sync, "async_stop", wraps=sync.async_stop) as stop:
        assert await hass.config_entries.async_unload(entry.entry_id)
    stop.assert_awaited_once()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_auth_failure_starts_reauth(recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get("https://hh.test/api/v1/ha/config", status=401, json={"error": "bad token"})
    entry = make_entry(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows), flows
    assert entry.error_reason_translation_key == "auth_failed"  # ConfigEntryAuthFailed(translation_key=...)


@pytest.mark.parametrize("kwargs", [{"status": 503}, {"exc": aiohttp.ClientConnectionError()}])
async def test_unreachable_server_retries(recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, kwargs: dict) -> None:
    aioclient_mock.get("https://hh.test/api/v1/ha/config", **kwargs)
    entry = make_entry(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == "cannot_connect"  # ConfigEntryNotReady(translation_key=...)


async def test_server_roles_replace_a_stale_copy(recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker) -> None:
    entry = make_entry(hass, roles=[{"id": "old", "label": "Old", "unit": "W"}])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options[CONF_ROLES] == ROLES
    assert entry.options[CONF_MAPPING] == LOAD
    assert entry.state is ConfigEntryState.LOADED


async def test_removing_the_entry_deletes_the_cursor_store(recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, hass_storage: dict) -> None:
    entry = make_entry(hass)
    key = f"{DOMAIN}.{entry.entry_id}.cursors"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"house_load_w": {"entity_id": "sensor.load", "cursor": "2026-09-28T00:00:00+00:00"}}}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert key not in hass_storage


async def test_entity_ids_survive_a_hub_id_change(recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker) -> None:
    """Reauth after the hub is re-created re-points CONF_HUB_ID; entity ids must not move."""
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    registry = er.async_get(hass)
    before = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert before

    hass.config_entries.async_update_entry(entry, unique_id="hub-2", data={**entry.data, CONF_HUB_ID: "hub-2"})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    after = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert after == before, (before, after)


def no_statistics_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_no_statistics_house_load_w")


@pytest.mark.parametrize("action", ["unload", "remove"])
async def test_unloading_or_removing_the_entry_deletes_its_repair_issues(recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, action: str) -> None:
    entry = make_entry(hass)  # sensor.load does not exist: an issue is raised
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert no_statistics_issue(hass, entry)
    if action == "unload":
        assert await hass.config_entries.async_unload(entry.entry_id)
    else:
        assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert no_statistics_issue(hass, entry) is None


async def test_setup_removes_0_1_leftovers(recorder_mock: Recorder, hass: HomeAssistant, server: AiohttpClientMocker, hass_storage: dict) -> None:
    entry = make_entry(hass)
    queue = f"{DOMAIN}.{entry.entry_id}.queue"
    hass_storage[queue] = {"version": 1, "minor_version": 1, "key": queue, "data": [{"ts": "2026-09-01T00:00:00+00:00", "role": "house_load_w", "value": 1}]}
    registry = er.async_get(hass)
    old = registry.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_queued_samples", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert queue not in hass_storage
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_last_upload")  # the current entities stay
