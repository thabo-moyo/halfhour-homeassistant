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


class RetryLater(HalfhourError):
    """Unreachable, overloaded or rate-limited: keep the data, try later."""

    def __init__(self, retry_after: float | None = None, status: int | None = None) -> None:
        super().__init__(f"retry after {retry_after}s" if retry_after else "retry later")
        self.retry_after = retry_after
        self.status = status


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

    async def send(self, samples: list[dict[str, Any]]) -> int:
        body = await self._request("POST", "/ha/telemetry", {"samples": samples}, compress=True)
        return int(body.get("accepted", 0))

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
                raise RetryLater(_retry_after(resp.headers.get("Retry-After")), status=resp.status)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RetryLater() from err


def _retry_after(raw: str | None) -> float | None:
    try:
        return float(raw) if raw else None
    except ValueError:
        return None
