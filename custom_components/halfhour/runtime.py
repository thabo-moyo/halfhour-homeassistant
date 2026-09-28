"""Sample every minute, upload every five, and keep the queue safe between."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import AuthError, HalfhourClient, RejectedError, RetryLater
from .const import BACKOFF_MAX, BACKOFF_START, CONF_MAPPING, DRAIN_DELAY, MAX_BATCH, SAMPLE_INTERVAL, UPLOAD_INTERVAL
from .queue import SampleQueue
from .sampler import collect

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


class HalfhourRuntime:
    """One paired home's sampler and uploader."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: HalfhourClient, queue: SampleQueue) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.queue = queue
        self.connected = False
        self.last_upload: datetime | None = None
        self._backoff = 0
        self._next_try: datetime | None = None
        self._uploading = False
        self._auth_failed = False
        self._unsubs: list[CALLBACK_TYPE] = []
        self._later: CALLBACK_TYPE | None = None
        self._listeners: list[Callable[[], None]] = []

    @callback
    def async_start(self) -> None:
        self._unsubs.append(async_track_time_interval(self.hass, self._on_sample_tick, SAMPLE_INTERVAL))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_upload_tick, UPLOAD_INTERVAL))
        self.entry.async_create_background_task(self.hass, self.async_upload(), "halfhour first upload")

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_later()
        await self.queue.async_flush()

    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def sample_now(self) -> None:
        now = dt_util.utcnow()
        # Sampling continues even while auth is failed and uploads are
        # blocked, so expiry can't live only in async_upload's happy path
        # (it returns before reaching queue.expire while _auth_failed is
        # set) or the queue grows without bound until reauth completes.
        self.queue.expire(now)
        self.queue.add(collect(self.hass, self.entry.options.get(CONF_MAPPING, {}), now))
        self._notify()

    async def async_upload(self) -> None:
        """Send the oldest batch; schedule the next if a backlog remains."""
        if self._uploading or self._auth_failed:
            return
        now = dt_util.utcnow()
        if self._next_try is not None and now < self._next_try:
            return
        self._uploading = True
        try:
            self.queue.expire(now)
            batch = self.queue.peek(MAX_BATCH)
            if not batch:
                return
            try:
                await self.client.send(batch)
            except AuthError:
                _LOGGER.warning("Halfhour refused this home's token; pair again to resume uploads")
                self._auth_failed = True
                self.connected = False
                self.entry.async_start_reauth(self.hass)
                return
            except RejectedError as err:
                _LOGGER.warning("Halfhour refused %d samples, dropping them: %s", len(batch), err)
                self.queue.drop(len(batch))
            except RetryLater as err:
                self.connected = False
                self._backoff = min(BACKOFF_MAX, self._backoff * 2 if self._backoff else BACKOFF_START)
                wait = max(self._backoff, err.retry_after or 0)
                self._next_try = now + timedelta(seconds=wait)
                self._schedule(wait)
                return
            else:
                self.queue.drop(len(batch))
                self.connected = True
                self.last_upload = dt_util.utcnow()
                self._backoff = 0
                self._next_try = None
            if len(self.queue):
                self._schedule(DRAIN_DELAY)
        finally:
            self._uploading = False
            self._notify()

    @callback
    def _on_sample_tick(self, _now: datetime) -> None:
        self.sample_now()

    async def _on_upload_tick(self, _now: datetime) -> None:
        await self.async_upload()

    @callback
    def _schedule(self, seconds: float) -> None:
        self._cancel_later()

        async def _run(_now: datetime) -> None:
            self._later = None
            await self.async_upload()

        self._later = async_call_later(self.hass, seconds, _run)

    @callback
    def _cancel_later(self) -> None:
        if self._later is not None:
            self._later()
            self._later = None

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()
