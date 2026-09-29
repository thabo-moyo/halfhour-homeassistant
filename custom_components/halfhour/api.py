"""HTTP client for the Halfhour gateway. Outbound only."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from typing import Any

import aiohttp


class HalfhourError(Exception):
    """Base error."""


class AuthError(HalfhourError):
    """The device token was refused: the home must pair again."""


class PairingError(HalfhourError):
    """The pairing code was bad, used or expired."""


class RejectedError(HalfhourError):
    """The request itself was refused; resending it cannot help."""


class DeviceLimitError(HalfhourError):
    """The account's plan has no free device slot (409 on /ha/pair)."""


class ConflictError(HalfhourError):
    """A device edit raced another (409 on /ha/devices/{id}): device is the current version, to edit again."""

    def __init__(self, detail: str, device: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.device = device


class NotFoundError(HalfhourError):
    """The device was deleted elsewhere (404 on /ha/devices/{id})."""


class RetryLater(HalfhourError):
    """Unreachable, overloaded or rate-limited: keep the data, try later."""

    def __init__(
        self, retry_after: float | None = None, status: int | None = None, detail: str | None = None, body: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"retry after {retry_after}s" if retry_after else "retry later")
        self.retry_after = retry_after
        self.status = status
        self.detail = detail
        self.body = body or {}  # the error response's JSON object, when it had one


@dataclass(frozen=True)
class SendResult:
    """A telemetry upload's answer: how many slots were taken, and the device roles dropped unknown."""

    accepted: int
    dropped_roles: tuple[str, ...]


@dataclass(frozen=True)
class PairResult:
    token: str
    hub_id: str
    account_name: str


class HalfhourClient:
    """Talks to {base_url}/api/v1 with an optional device token."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str | None = None) -> None:
        self._session = session
        self._base = base_url.rstrip("/") + "/api/v1"
        self._token = token

    async def pair(self, code: str, instance_id: str, ha_version: str) -> PairResult:
        try:
            body = await self._request("POST", "/ha/pair", {"code": code, "instance_id": instance_id, "ha_version": ha_version})
        except RejectedError as err:
            raise PairingError(str(err)) from err
        except RetryLater as err:
            if err.status == 409:
                raise DeviceLimitError(str(err)) from err
            raise
        return PairResult(body["token"], body["hub_id"], body["account_name"])

    async def config(self) -> dict[str, Any]:
        return await self._request("GET", "/ha/config")

    async def send(self, slots: list[dict[str, Any]]) -> SendResult:
        """Upload slots. A dev. role the gateway no longer knows (a deleted device) comes back in dropped_roles."""
        body = await self._request("POST", "/ha/telemetry", {"slots": slots}, compress=True)
        dropped = body.get("dropped_roles")
        roles = tuple(r for r in dropped if isinstance(r, str)) if isinstance(dropped, list) else ()
        return SendResult(int(body.get("accepted", 0)), roles)

    async def put_inventory(self, entities: list[dict[str, Any]]) -> None:
        """Replace the hub's entity inventory (metadata only) at Halfhour."""
        await self._request("PUT", "/ha/inventory", {"v": 1, "entities": entities})

    async def device_types(self) -> dict[str, Any]:
        """GET /device-types (public): {items, hub_kinds}; the device forms are built from hub_kinds."""
        return await self._request("GET", "/device-types")

    async def create_device(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._device_request("POST", "/ha/devices", body)

    async def update_device(self, device_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._device_request("PUT", f"/ha/devices/{device_id}", body)

    async def delete_device(self, device_id: str) -> None:
        await self._device_request("DELETE", f"/ha/devices/{device_id}")

    async def _device_request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await self._request(method, path, body)
        except RetryLater as err:
            detail = err.detail or str(err)
            if err.status == 404:
                raise NotFoundError(detail) from err
            if err.status == 409:
                device = err.body.get("device")
                if isinstance(device, dict):
                    raise ConflictError(detail, device) from err
                if method == "POST":
                    raise DeviceLimitError(detail) from err  # the plan has no free device slot
                raise ConflictError(detail) from err
            raise

    async def _request(self, method: str, path: str, payload: Any = None, compress: bool = False) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        kwargs: dict[str, Any] = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=30)}
        if payload is not None and compress:
            headers["Content-Type"] = "application/json"
            headers["Content-Encoding"] = "gzip"
            kwargs["data"] = gzip.compress(json.dumps(payload).encode())
        elif payload is not None:
            kwargs["json"] = payload
        try:
            async with self._session.request(method, self._base + path, **kwargs) as resp:
                raw = await resp.text()
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                body = body if isinstance(body, dict) else {}
                if resp.status < 400:
                    return body
                detail = str(body.get("detail", getattr(resp, "reason", None) or resp.status))
                if resp.status == 401:
                    raise AuthError(detail)
                # Only these mean "this exact request is bad, don't resend it
                # unchanged": everything else in 4xx (403, 404, 405, 408, 409,
                # 429, ...) and 5xx is transient from the Integration's point
                # of view, so it keeps the batch and retries later.
                if resp.status in (400, 413, 415, 422):
                    raise RejectedError(detail)
                raise RetryLater(_retry_after(resp.headers.get("Retry-After")), status=resp.status, detail=detail, body=body)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RetryLater() from err


def _retry_after(raw: str | None) -> float | None:
    try:
        return float(raw) if raw else None
    except ValueError:
        return None
