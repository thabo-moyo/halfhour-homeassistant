"""The Halfhour HTTP client."""

import gzip
import json

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.halfhour.api import (
    AuthError,
    DeviceLimitError,
    HalfhourClient,
    PairingError,
    RejectedError,
    RetryLater,
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
    samples = [{"ts": "2026-09-27T10:00:00+00:00", "role": "house_load_w", "value": 400.0}]
    assert await client.send(samples) == 1
    _, _, data, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == "Bearer hh_dev_tok"
    assert headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(data)) == {"samples": samples}


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
