"""The Halfhour integration: send this home's half-hour readings to Halfhour, show its live Plan."""

from __future__ import annotations

import logging
from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from . import stats
from .api import AuthError, HalfhourClient, HalfhourError
from .channel import HalfhourChannel, url_allowed
from .const import BROKER_RECHECK, CONF_ROLES, CONF_TOKEN, CONF_URL, DEFAULT_STALE_AFTER, DOMAIN, STORAGE_VERSION
from .runtime import HalfhourRuntime, Plans, ReloadKey, plan_store
from .sync import HalfhourSync, async_delete_issues

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

type HalfhourConfigEntry = ConfigEntry[HalfhourRuntime]


async def async_setup_entry(hass: HomeAssistant, entry: HalfhourConfigEntry) -> bool:
    client = HalfhourClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    try:
        config = await client.config()
    except AuthError as err:
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="auth_failed") from err
    except HalfhourError as err:
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="cannot_connect") from err
    _update_roles(hass, entry, config["roles"])
    await _async_remove_0_1_leftovers(hass, entry)
    mqtt = _mqtt(config)

    sync = HalfhourSync(hass, entry, client, partial(stats.fetch, hass))
    await sync.async_load()
    plans = Plans(hass, entry.entry_id, _stale_after(mqtt.get("stale_after_s")))
    await plans.async_load()
    runtime = HalfhourRuntime(sync, None, plans, _reload_key(entry), _broker(mqtt))
    url = runtime.broker[0]
    if url is not None:

        async def on_command(name: str, args: dict[str, Any]) -> None:
            await _async_command(hass, entry, client, name, args)

        async def verify_key() -> bool:
            # The broker's "not authorised" can also mean its auth hook is down:
            # only Halfhour itself refusing the token is a revoked key.
            try:
                await client.config()
            except AuthError:
                return False
            except HalfhourError:
                return True  # unreachable or busy: can't tell, so not a revocation
            return True

        runtime.channel = HalfhourChannel(hass, entry, url, _account(mqtt), plans.offer, on_command, verify_key)
    else:
        ir.async_delete_issue(hass, DOMAIN, _insecure_issue(entry))
    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    sync.async_start()
    plans.async_start()
    if url is None or not url_allowed(url):
        # No channel will connect, and none tells this home when that changes:
        # read the config again now and then, and reload once a usable broker appears.
        async def _recheck(_now: datetime) -> None:
            try:
                await _async_refresh(hass, entry, client)
            except HalfhourError as err:
                _LOGGER.debug("Re-reading Halfhour's config failed: %s", err)

        entry.async_on_unload(async_track_time_interval(hass, _recheck, BROKER_RECHECK))
    if runtime.channel is not None:
        # Not tied to the entry: an unload must not cancel it mid-connect, or the
        # channel's own "stopped while connecting" check never runs.
        hass.async_create_background_task(runtime.channel.async_start(), "halfhour live channel")
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HalfhourConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        runtime = entry.runtime_data
        if runtime.channel is not None:
            await runtime.channel.async_stop()
        await runtime.plans.async_stop()
        await runtime.sync.async_stop()
        async_delete_issues(hass, entry.entry_id)
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: HalfhourConfigEntry) -> None:
    """Forget the sync cursors, the Plan and the repair issues when the home is removed."""
    async_delete_issues(hass, entry.entry_id)
    ir.async_delete_issue(hass, DOMAIN, _insecure_issue(entry))
    # The same keys HalfhourSync and Plans store under; no client is needed to delete them.
    await Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.cursors").async_remove()
    await plan_store(hass, entry.entry_id).async_remove()


async def _async_reload(hass: HomeAssistant, entry: HalfhourConfigEntry) -> None:
    if _reload_key(entry) == entry.runtime_data.reload_key:
        return  # only the server's role list changed: nothing running depends on it
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_command(hass: HomeAssistant, entry: HalfhourConfigEntry, client: HalfhourClient, name: str, args: dict[str, Any]) -> None:
    """Run one command from Halfhour; raising reports it back as failed."""
    runtime = entry.runtime_data
    if name == "resync":
        await runtime.sync.async_resync()
    elif name == "reload_config":
        if isinstance(args.get("roles"), list):
            # The retained config message carries what /ha/config would say, less the broker.
            _apply_config(hass, entry, args)
            return
        await _async_refresh(hass, entry, client)
    else:
        raise ValueError(f"unknown command {name!r}")


async def _async_refresh(hass: HomeAssistant, entry: HalfhourConfigEntry, client: HalfhourClient) -> None:
    """Fetch /ha/config and apply it; a new broker or account reloads the entry to reconnect."""
    config = await client.config()
    mqtt = _mqtt(config)
    _apply_config(hass, entry, {**config, "stale_after_s": mqtt.get("stale_after_s")})
    if _broker(mqtt) != entry.runtime_data.broker:
        _LOGGER.info("Halfhour's live connection details changed; reloading")
        hass.config_entries.async_schedule_reload(entry.entry_id)


def _apply_config(hass: HomeAssistant, entry: HalfhourConfigEntry, config: dict[str, Any]) -> None:
    if isinstance(config.get("roles"), list):
        _update_roles(hass, entry, config["roles"])
    entry.runtime_data.plans.set_stale_after(_stale_after(config.get("stale_after_s")))


def _update_roles(hass: HomeAssistant, entry: HalfhourConfigEntry, roles: Any) -> None:
    if roles != entry.options.get(CONF_ROLES):
        # The server owns the role list; keep a fresh copy so options need no network.
        hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_ROLES: roles})


def _mqtt(config: dict[str, Any]) -> dict[str, Any]:
    mqtt = config.get("mqtt")
    return mqtt if isinstance(mqtt, dict) else {}


def _broker(mqtt: dict[str, Any]) -> tuple[str | None, str | None]:
    url = mqtt.get("url")
    return (url if isinstance(url, str) and url else None), _account(mqtt)


def _account(mqtt: dict[str, Any]) -> str | None:
    account = mqtt.get("account")
    return account if isinstance(account, str) and account else None


def _stale_after(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_STALE_AFTER


def _reload_key(entry: HalfhourConfigEntry) -> ReloadKey:
    return dict(entry.data), {k: v for k, v in entry.options.items() if k != CONF_ROLES}


def _insecure_issue(entry: HalfhourConfigEntry) -> str:
    return f"{entry.entry_id}_insecure_broker"  # as HalfhourChannel raises it


async def _async_remove_0_1_leftovers(hass: HomeAssistant, entry: HalfhourConfigEntry) -> None:
    """0.1.x kept a sample queue and a queued-samples sensor; 0.2 has neither."""
    await Store[Any](hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.queue").async_remove()
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(Platform.SENSOR, DOMAIN, f"{entry.entry_id}_queued_samples"):
        registry.async_remove(entity_id)
