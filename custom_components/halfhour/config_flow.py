"""Pair with a one-time code, then map this home's sensors to roles."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector

from .api import DeviceLimitError, HalfhourClient, HalfhourError, PairingError, PairResult
from .const import CONF_CODE, CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DEFAULT_URL, DOMAIN
from .mapping import mapping_from_input, mapping_schema, picked, suggest, suggested_from_mapping
from .stats import has_statistics


async def _check(hass: HomeAssistant, roles: list[dict[str, Any]], user_input: dict[str, Any]) -> dict[str, str]:
    """A form error for each picked entity the recorder keeps no statistics for."""
    return {rid: "no_statistics" for rid, entity_id in picked(roles, user_input).items() if not await has_statistics(hass, entity_id)}


class HalfhourConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._url = DEFAULT_URL
        self._paired: PairResult | None = None
        self._config: dict[str, Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HalfhourOptionsFlow:
        return HalfhourOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                paired = await self._pair(user_input[CONF_URL], user_input[CONF_CODE])
            except PairingError:
                errors["base"] = "invalid_code"
            except DeviceLimitError:
                errors["base"] = "device_limit"
            except HalfhourError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(paired.hub_id)
                # /ha/pair for an already-paired instance keeps the hub but
                # rotates its token: hand the working entry the new token
                # rather than discarding it and aborting blind.
                self._abort_if_unique_id_configured(updates={CONF_TOKEN: paired.token})
                self._url, self._paired = user_input[CONF_URL], paired
                return await self.async_step_mapping()
        schema = vol.Schema({vol.Required(CONF_URL, default=self._url): TextSelector(), vol.Required(CONF_CODE): TextSelector()})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_mapping(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._paired is not None
        if self._config is None:
            try:
                self._config = await HalfhourClient(async_get_clientsession(self.hass), self._url, self._paired.token).config()
            except HalfhourError:
                return self.async_abort(reason="cannot_connect")
        roles: list[dict[str, Any]] = self._config["roles"]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _check(self.hass, roles, user_input)
            if not errors:
                return self.async_create_entry(
                    title=self._paired.account_name,
                    data={CONF_URL: self._url, CONF_TOKEN: self._paired.token, CONF_HUB_ID: self._paired.hub_id},
                    options={CONF_MAPPING: mapping_from_input(self.hass, roles, user_input), CONF_ROLES: roles},
                )
            suggested: dict[str, Any] = user_input
        else:
            suggested = dict(suggest(self.hass.states.async_entity_ids("sensor"), self._config["presets"]))
        return self.async_show_form(step_id="mapping", data_schema=mapping_schema(roles, suggested), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                paired = await self._pair(entry.data[CONF_URL], user_input[CONF_CODE])
            except PairingError:
                errors["base"] = "invalid_code"
            except DeviceLimitError:
                errors["base"] = "device_limit"
            except HalfhourError:
                errors["base"] = "cannot_connect"
            else:
                other = await self.async_set_unique_id(paired.hub_id)
                if other is not None and other.entry_id != entry.entry_id:
                    # This HA is already paired to that account as another entry, and
                    # /ha/pair just rotated its token: hand it the new one, then abort.
                    self._abort_if_unique_id_configured(updates={CONF_TOKEN: paired.token})
                # Same hub (token-only re-pair) or a new hub after the old one was
                # deleted in Halfhour: re-point this entry, keep its queue and mapping.
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=paired.hub_id,
                    title=paired.account_name,
                    data_updates={CONF_TOKEN: paired.token, CONF_HUB_ID: paired.hub_id},
                )
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({vol.Required(CONF_CODE): TextSelector()}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Move this home to another Halfhour address; a token belongs to one server, so pair again."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            try:
                paired = await self._pair(user_input[CONF_URL], user_input[CONF_CODE])
            except PairingError:
                errors["base"] = "invalid_code"
            except DeviceLimitError:
                errors["base"] = "device_limit"
            except HalfhourError:
                errors["base"] = "cannot_connect"
            else:
                other = await self.async_set_unique_id(paired.hub_id)
                if other is not None and other.entry_id != entry.entry_id:
                    # As in reauth: /ha/pair rotated that entry's token, so hand it over.
                    self._abort_if_unique_id_configured(updates={CONF_TOKEN: paired.token})
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=paired.hub_id,
                    title=paired.account_name,
                    data_updates={CONF_URL: user_input[CONF_URL], CONF_TOKEN: paired.token, CONF_HUB_ID: paired.hub_id},
                )
        schema = vol.Schema({vol.Required(CONF_URL, default=entry.data[CONF_URL]): TextSelector(), vol.Required(CONF_CODE): TextSelector()})
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    async def _pair(self, url: str, code: str) -> PairResult:
        client = HalfhourClient(async_get_clientsession(self.hass), url)
        return await client.pair(code.strip(), await instance_id.async_get(self.hass), HA_VERSION)


class HalfhourOptionsFlow(OptionsFlow):
    """Re-map sensors without pairing again."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        roles: list[dict[str, Any]] = self.config_entry.options[CONF_ROLES]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _check(self.hass, roles, user_input)
            if not errors:
                return self.async_create_entry(data={CONF_MAPPING: mapping_from_input(self.hass, roles, user_input), CONF_ROLES: roles})
            suggested: dict[str, Any] = user_input
        else:
            suggested = suggested_from_mapping(self.config_entry.options.get(CONF_MAPPING, {}))
        return self.async_show_form(step_id="init", data_schema=mapping_schema(roles, suggested), errors=errors)
