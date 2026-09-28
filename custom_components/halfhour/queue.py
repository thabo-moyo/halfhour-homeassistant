"""Samples waiting to be sent, mirrored to .storage so a restart loses none."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MAX_AGE, STORAGE_VERSION

SAVE_DELAY = 30  # s: at most this much is lost if HA dies uncleanly


class SampleQueue:
    """Oldest-first list of samples, persisted with a debounced save."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.queue")
        self._items: list[dict[str, Any]] = []

    async def async_load(self) -> None:
        data = await self._store.async_load()
        self._items = list(data["samples"]) if data else []

    def __len__(self) -> int:
        return len(self._items)

    def add(self, samples: list[dict[str, Any]]) -> None:
        if samples:
            self._items.extend(samples)
            self._save()

    def expire(self, now: datetime) -> None:
        """Drop samples older than MAX_AGE: the gateway would refuse them."""
        cutoff = now - MAX_AGE
        kept = [s for s in self._items if (t := dt_util.parse_datetime(s["ts"])) is not None and t >= cutoff]
        if len(kept) != len(self._items):
            self._items = kept
            self._save()

    def peek(self, n: int) -> list[dict[str, Any]]:
        return self._items[:n]

    def drop(self, n: int) -> None:
        del self._items[:n]
        self._save()

    async def async_flush(self) -> None:
        await self._store.async_save({"samples": self._items})

    async def async_remove(self) -> None:
        await self._store.async_remove()

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"samples": self._items}, SAVE_DELAY)
