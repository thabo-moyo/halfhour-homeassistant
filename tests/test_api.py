"""The Halfhour HTTP client."""

import gzip
import json

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.halfhour.api import (
    AuthError,
    ConflictError,
    DeviceLimitError,
    HalfhourClient,
    NotFoundError,
    PairingError,
    RejectedError,
    RetryLater,
    SendResult,
)

BASE = "https://hh.test"


async def test_pair_returns_token(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE}/api/v1/ha/pair", status=201, json={"token": "hh_dev_x", "hub_id": "hub-1", "account_name": "Sam"})
    client = HalfhourClient(async_get_clientsession(hass), BASE + "/")
    result = await client.pair("ABCD2345", "inst", "2026.9.1")
    assert (result.token, result.hub_id, result.account_name) == ("hh_dev_x", "hub-1", "Sam")
    _, _, body, _ = aioclient_mock.mock_calls[0]
    assert body == {"code": "ABCD2345", "instance_id": "inst", "ha_version": "2026.9.1"}


async def test_pair_bad_code(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE}/api/v1/ha/pair", status=422, json={"detail": "That code isn't valid."})
    with pytest.raises(PairingError):
        await HalfhourClient(async_get_clientsession(hass), BASE).pair("X", "i", "v")


async def test_pair_device_limit_is_a_distinct_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE}/api/v1/ha/pair", status=409, json={"detail": "Your Insight plan covers 1 device."})
    with pytest.raises(DeviceLimitError):
        await HalfhourClient(async_get_clientsession(hass), BASE).pair("X", "i", "v")


async def test_send_gzips_with_bearer(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE}/api/v1/ha/telemetry", status=202, json={"accepted": 1})
    client = HalfhourClient(async_get_clientsession(hass), BASE, "hh_dev_tok")
    slots = [{"slot": "2026-09-27T10:00:00+00:00", "role": "house_load_w", "value": 400.0, "coverage_s": 1800, "resolution_s": 300}]
    assert await client.send(slots) == SendResult(1, ())
    _, _, data, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == "Bearer hh_dev_tok"
    assert headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(data)) == {"slots": slots}


async def test_send_reports_the_device_roles_the_gateway_dropped(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE}/api/v1/ha/telemetry", status=202, json={"accepted": 1, "dropped_roles": ["dev.x.soc", 3, "dev.y.power"]})
    got = await HalfhourClient(async_get_clientsession(hass), BASE, "t").send([])
    assert got == SendResult(1, ("dev.x.soc", "dev.y.power"))


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (401, {}, AuthError),
        (400, {}, RejectedError),
        (413, {}, RejectedError),
        (422, {}, RejectedError),
        (404, {}, RetryLater),
        (409, {}, RetryLater),
        (429, {"Retry-After": "7"}, RetryLater),
        (503, {"Retry-After": "60"}, RetryLater),
        (500, {}, RetryLater),
    ],
)
async def test_send_maps_statuses(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status, headers, error):
    aioclient_mock.post(f"{BASE}/api/v1/ha/telemetry", status=status, json={"detail": "x"}, headers=headers)
    with pytest.raises(error) as info:
        await HalfhourClient(async_get_clientsession(hass), BASE, "t").send([])
    if headers:
        assert info.value.retry_after == float(headers["Retry-After"])


async def test_connection_error_is_retry_later(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE}/api/v1/ha/telemetry", exc=TimeoutError())
    with pytest.raises(RetryLater):
        await HalfhourClient(async_get_clientsession(hass), BASE, "t").send([])


async def test_pair_unreachable_stays_retry_later(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE}/api/v1/ha/pair", status=503)
    with pytest.raises(RetryLater) as info:
        await HalfhourClient(async_get_clientsession(hass), BASE).pair("X", "i", "v")
    assert not isinstance(info.value, DeviceLimitError)
    assert info.value.status == 503


async def test_config_returns_roles_and_presets(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(f"{BASE}/api/v1/ha/config", json={"roles": [], "presets": []})
    assert await HalfhourClient(async_get_clientsession(hass), BASE, "t").config() == {"roles": [], "presets": []}


@pytest.mark.parametrize("text", ["not json", "[1, 2]"])
async def test_non_object_body_reads_as_empty(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, text):
    aioclient_mock.get(f"{BASE}/api/v1/ha/config", text=text)
    assert await HalfhourClient(async_get_clientsession(hass), BASE, "t").config() == {}


async def test_unparseable_retry_after_is_ignored(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE}/api/v1/ha/telemetry", status=429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    with pytest.raises(RetryLater) as info:
        await HalfhourClient(async_get_clientsession(hass), BASE, "t").send([])
    assert info.value.retry_after is None


# -- inventory and devices behind the hub ----------------------------------------


async def test_put_inventory_sends_v1_entities_with_bearer(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.put(f"{BASE}/api/v1/ha/inventory", status=204)
    client = HalfhourClient(async_get_clientsession(hass), BASE, "hh_dev_tok")
    entities = [{"entity_id": "sensor.a", "name": "A", "domain": "sensor"}]
    await client.put_inventory(entities)
    method, url, body, headers = aioclient_mock.mock_calls[0]
    assert (method, str(url)) == ("PUT", f"{BASE}/api/v1/ha/inventory")
    assert body == {"v": 1, "entities": entities}
    assert headers["Authorization"] == "Bearer hh_dev_tok"


async def test_device_create_update_delete(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    dev = "3f2a0000-0000-4000-8000-000000000001"
    aioclient_mock.post(f"{BASE}/api/v1/ha/devices", status=201, json={"id": dev, "revision": 2})
    aioclient_mock.put(f"{BASE}/api/v1/ha/devices/{dev}", json={"id": dev, "revision": 3})
    aioclient_mock.delete(f"{BASE}/api/v1/ha/devices/{dev}", status=204)
    client = HalfhourClient(async_get_clientsession(hass), BASE, "t")
    body = {"kind": "load", "name": "Immersion", "mapping": {}, "facts": {}}
    assert await client.create_device(body) == {"id": dev, "revision": 2}
    assert await client.update_device(dev, body) == {"id": dev, "revision": 3}
    await client.delete_device(dev)
    assert [(m, str(u).removeprefix(BASE)) for m, u, _, _ in aioclient_mock.mock_calls] == [
        ("POST", "/api/v1/ha/devices"),
        ("PUT", f"/api/v1/ha/devices/{dev}"),
        ("DELETE", f"/api/v1/ha/devices/{dev}"),
    ]
    assert aioclient_mock.mock_calls[0][2] == body


async def test_a_device_conflict_carries_the_current_device(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    current = {"id": "x", "kind": "load", "name": "Immersion", "revision": 4}
    aioclient_mock.put(f"{BASE}/api/v1/ha/devices/x", status=409, json={"title": "Conflict", "detail": "This device changed.", "device": current})
    with pytest.raises(ConflictError, match="This device changed") as err:
        await HalfhourClient(async_get_clientsession(hass), BASE, "t").update_device("x", {"revision": 3})
    assert err.value.device == current


async def test_a_put_conflict_without_a_device_has_none(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.put(f"{BASE}/api/v1/ha/devices/x", status=409, json={"detail": "busy"})
    with pytest.raises(ConflictError) as err:
        await HalfhourClient(async_get_clientsession(hass), BASE, "t").update_device("x", {})
    assert err.value.device is None


async def test_a_create_over_the_plan_limit_is_a_device_limit(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE}/api/v1/ha/devices", status=409, json={"title": "Conflict", "detail": "Your Insight plan covers 1 device. Change plan to link more."})
    with pytest.raises(DeviceLimitError, match="Insight plan"):
        await HalfhourClient(async_get_clientsession(hass), BASE, "t").create_device({})


@pytest.mark.parametrize("method", ["update", "delete"])
async def test_a_device_deleted_elsewhere_is_not_found(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, method: str):
    aioclient_mock.put(f"{BASE}/api/v1/ha/devices/x", status=404, json={"detail": "no such device"})
    aioclient_mock.delete(f"{BASE}/api/v1/ha/devices/x", status=404, json={"detail": "no such device"})
    client = HalfhourClient(async_get_clientsession(hass), BASE, "t")
    with pytest.raises(NotFoundError, match="no such device"):
        if method == "update":
            await client.update_device("x", {})
        else:
            await client.delete_device("x")


async def test_a_refused_device_names_the_field(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE}/api/v1/ha/devices", status=422, json={"detail": "mapping.soc: entity not found"})
    with pytest.raises(RejectedError, match="mapping.soc"):
        await HalfhourClient(async_get_clientsession(hass), BASE, "t").create_device({})


async def test_an_unreachable_gateway_on_a_device_edit_is_retry_later(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.delete(f"{BASE}/api/v1/ha/devices/x", status=503)
    with pytest.raises(RetryLater):
        await HalfhourClient(async_get_clientsession(hass), BASE, "t").delete_device("x")


async def test_device_types_returns_the_hub_kinds(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    kinds = [{"kind": "load", "label": "Load", "fields": [], "facts": []}]
    aioclient_mock.get(f"{BASE}/api/v1/device-types", json={"items": [{"kind": "battery"}], "hub_kinds": kinds})
    assert await HalfhourClient(async_get_clientsession(hass), BASE, "t").device_types() == {"items": [{"kind": "battery"}], "hub_kinds": kinds}
