"""Diagnostics download: entry and queue state, token redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN

TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    rt = entry.runtime_data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "queue": {"length": len(rt.queue)},
        "connected": rt.connected,
        "last_upload": rt.last_upload.isoformat() if rt.last_upload else None,
    }
