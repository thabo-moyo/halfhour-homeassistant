"""Diagnostics download: entry, sync, channel and Plan state, token redacted."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_TOKEN

if TYPE_CHECKING:
    from . import HalfhourConfigEntry

TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: HalfhourConfigEntry) -> dict[str, Any]:
    runtime = entry.runtime_data
    sync, channel, plans = runtime.sync, runtime.channel, runtime.plans
    synced_until = sync.synced_until()
    devices_synced_until = sync.devices_synced_until()
    now = dt_util.utcnow()
    plan = plans.plan
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "cursors": sync.cursors,
        "synced_until": synced_until.isoformat() if synced_until else None,
        # Devices backfill apart from the house roles: a device added today reads back a year.
        "devices_synced_until": devices_synced_until.isoformat() if devices_synced_until else None,
        "connected": sync.connected,
        "last_upload": sync.last_upload.isoformat() if sync.last_upload else None,
        "channel": (
            {"url": channel.url, "account": channel.account, "live": channel.live, "command_ids": channel.command_ids} if channel is not None else None
        ),
        "plan": (
            {
                "id": plan.id,
                "made_at": plan.made_at.isoformat(),
                "age_s": round((now - plan.made_at).total_seconds()),
                "slots": len(plan.slots),
                "stale": plans.stale(now),
                "stale_after_s": plans.stale_after_s,
            }
            if plan is not None
            else None
        ),
    }
