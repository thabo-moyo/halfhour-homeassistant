"""Diagnostics download: entry and sync state, token redacted."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN

if TYPE_CHECKING:
    from . import HalfhourConfigEntry

TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: HalfhourConfigEntry) -> dict[str, Any]:
    sync = entry.runtime_data
    synced_until = sync.synced_until()
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "cursors": sync.cursors,
        "synced_until": synced_until.isoformat() if synced_until else None,
        "connected": sync.connected,
        "last_upload": sync.last_upload.isoformat() if sync.last_upload else None,
    }
