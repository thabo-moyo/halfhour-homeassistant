"""Pair with a one-time code, then map this home's sensors to roles; options add, edit and remove devices."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig, TextSelector

from .api import ConflictError, DeviceLimitError, HalfhourClient, HalfhourError, NotFoundError, PairingError, PairResult, RejectedError
from .const import CONF_CODE, CONF_HUB_ID, CONF_MAPPING, CONF_ROLES, CONF_TOKEN, CONF_URL, DEFAULT_URL, DOMAIN
from .device_form import device_body, field_error, find_kind, form_schema, kind_options, suggested_for_new, suggested_from_device, valid_kinds
from .devices import HubDevice, HubDevices
from .mapping import mapping_from_input, mapping_schema, picked, suggest, suggested_from_mapping
from .runtime import HalfhourRuntime
from .stats import has_statistics

_LOGGER = logging.getLogger(__name__)

LIST_WAIT = 5.0  # s an options flow waits for the device list carrying its change


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
    """Re-map sensors, or add, edit and remove the devices behind this home, without pairing again."""

    def __init__(self) -> None:
        self._kind: dict[str, Any] = {}
        self._device_id = ""
        self._device_name = ""
        self._revision: int | None = None
        self._suggested: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["sensors", "add_device"]
        if self._listed():
            options += ["edit_device", "remove_device"]
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        roles: list[dict[str, Any]] = self.config_entry.options[CONF_ROLES]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _check(self.hass, roles, user_input)
            if not errors:
                return self.async_create_entry(data={CONF_MAPPING: mapping_from_input(self.hass, roles, user_input), CONF_ROLES: roles})
            suggested: dict[str, Any] = user_input
        else:
            suggested = suggested_from_mapping(self.config_entry.options.get(CONF_MAPPING, {}))
        return self.async_show_form(step_id="sensors", data_schema=mapping_schema(roles, suggested), errors=errors)

    # -- devices behind this home -----------------------------------------------------

    async def async_step_add_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        kinds, unusable = await self._kinds()
        if unusable:
            return self.async_abort(reason=unusable)
        errors: dict[str, str] = {}
        if user_input is not None:
            kind = find_kind(kinds, user_input["kind"])
            if kind is None:
                errors["kind"] = "kind_unavailable"  # Halfhour stopped serving it since the form was shown
            else:
                self._kind, self._suggested = kind, suggested_for_new(kind)
                return await self.async_step_add_details()
        schema = vol.Schema({vol.Required("kind"): SelectSelector(SelectSelectorConfig(options=kind_options(kinds)))})
        return self.async_show_form(step_id="add_device", data_schema=schema, errors=errors)

    async def async_step_add_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = self._placeholders()
        if user_input is not None:
            self._suggested = dict(user_input)
            body = device_body(self._kind, user_input)
            try:
                device = await self._client().create_device({"kind": self._kind["kind"], **body})
            except DeviceLimitError:
                errors["base"] = "device_limit"
            except RejectedError as err:
                errors.update(self._refused(str(err), placeholders))
            except HalfhourError:
                errors["base"] = "cannot_connect"
            else:
                await self._await_list(lambda held: _newer_or_same(held.revision, device.get("revision")))
                return self.async_abort(reason="device_added", description_placeholders={"name": str(device.get("name") or body["name"])})
        return self._details_form("add_details", errors, placeholders)

    async def async_step_edit_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        held = self._hub_devices()
        if user_input is not None and held is not None:
            device = held.device(user_input["device"])
            if device is None:
                return self.async_abort(reason="not_found")
            kinds, unusable = await self._kinds()
            if unusable:
                return self.async_abort(reason=unusable)
            kind = find_kind(kinds, device.kind)
            if kind is None:
                return self.async_abort(reason="unknown_kind")
            self._kind, self._device_id, self._device_name, self._revision = kind, device.id, device.name, held.revision
            self._suggested = suggested_from_device(kind, device.name, device.mapping, device.facts, device.label)
            return await self.async_step_edit_details()
        return self.async_show_form(step_id="edit_device", data_schema=self._device_picker())

    async def async_step_edit_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = self._placeholders()
        if user_input is not None:
            self._suggested = dict(user_input)
            body = device_body(self._kind, user_input)
            if self._revision is not None:
                body["revision"] = self._revision
            try:
                device = await self._client().update_device(self._device_id, body)
            except ConflictError as err:
                # Edited elsewhere since this form opened: show the latest and edit against it.
                errors["base"] = "stale_revision"
                if err.device is not None:
                    self._take_latest(err.device)
                    placeholders = self._placeholders()
            except NotFoundError:
                return self.async_abort(reason="not_found")
            except RejectedError as err:
                errors.update(self._refused(str(err), placeholders))
            except HalfhourError:
                errors["base"] = "cannot_connect"
            else:
                await self._await_list(lambda held: _newer_or_same(held.revision, device.get("revision")))
                return self.async_abort(reason="device_updated", description_placeholders={"name": str(device.get("name") or body["name"])})
        return self._details_form("edit_details", errors, placeholders)

    async def async_step_remove_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        held = self._hub_devices()
        if user_input is not None and held is not None:
            device_id = user_input["device"]
            device = held.device(device_id)
            try:
                await self._client().delete_device(device_id)
            except NotFoundError:
                return self.async_abort(reason="not_found")
            except HalfhourError:
                errors["base"] = "cannot_connect"
            else:
                await self._await_list(lambda now: now.device(device_id) is None)
                return self.async_abort(reason="device_removed", description_placeholders={"name": device.name if device else device_id})
        return self.async_show_form(step_id="remove_device", data_schema=self._device_picker(), errors=errors)

    # -- helpers ---------------------------------------------------------------------

    def _client(self) -> HalfhourClient:
        data = self.config_entry.data
        return HalfhourClient(async_get_clientsession(self.hass), data[CONF_URL], data[CONF_TOKEN])

    async def _kinds(self) -> tuple[list[dict[str, Any]], str | None]:
        """The kinds a device behind a hub can be, as Halfhour serves them now, or why there are none (an abort reason).

        A server from before devices behind a hub serves no hub_kinds at all.
        """
        try:
            body = await self._client().device_types()
        except HalfhourError:
            return [], "cannot_connect"
        if not isinstance(body.get("hub_kinds"), list):
            return [], "gateway_too_old"
        kinds = valid_kinds(body)
        return kinds, None if kinds else "cannot_connect"

    def _hub_devices(self) -> HubDevices | None:
        """The device list the loaded entry holds; None while the entry isn't loaded."""
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return None
        runtime: HalfhourRuntime = self.config_entry.runtime_data
        return runtime.devices

    def _listed(self) -> tuple[HubDevice, ...]:
        held = self._hub_devices()
        return held.devices.devices if held is not None and held.devices is not None else ()

    def _device_picker(self) -> vol.Schema:
        options = [SelectOptionDict(value=d.id, label=d.name) for d in self._listed()]
        return vol.Schema({vol.Required("device"): SelectSelector(SelectSelectorConfig(options=options))})

    def _placeholders(self) -> dict[str, str]:
        return {"kind": str(self._kind.get("label") or self._kind.get("kind", "")), "name": self._device_name, "field": "", "detail": ""}

    def _refused(self, detail: str, placeholders: dict[str, str]) -> dict[str, str]:
        """A 422 as a form error on the field it names, with the field and the gateway's reason as placeholders."""
        key, label, reason = field_error(self._kind, detail)
        placeholders.update(field=label, detail=reason)
        return {key: "invalid_mapping"}

    def _take_latest(self, device: dict[str, Any]) -> None:
        mapping, facts = device.get("mapping"), device.get("facts")
        self._device_name = str(device.get("name") or self._device_name)
        revision = device.get("revision")
        self._revision = revision if isinstance(revision, int) and not isinstance(revision, bool) else None
        label = device.get("label")
        self._suggested = suggested_from_device(
            self._kind,
            self._device_name,
            mapping if isinstance(mapping, dict) else {},
            facts if isinstance(facts, dict) else {},
            label if isinstance(label, str) else None,
        )

    def _details_form(self, step_id: str, errors: dict[str, str], placeholders: dict[str, str]) -> ConfigFlowResult:
        schema = self.add_suggested_values_to_schema(form_schema(self._kind), self._suggested)
        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors, description_placeholders=placeholders)

    async def _await_list(self, arrived: Callable[[HubDevices], bool]) -> None:
        """Wait (at most LIST_WAIT s) for the device list carrying this change, so the flow ends on fresh state.

        The gateway publishes the list as it answers; if it doesn't come in
        time (no live connection, say), finish anyway: it arrives later.
        """
        held = self._hub_devices()
        if held is None or arrived(held):
            return
        event = asyncio.Event()

        @callback
        def _changed() -> None:
            if arrived(held):
                event.set()

        unsub = held.add_listener(_changed)
        try:
            async with asyncio.timeout(LIST_WAIT):
                await event.wait()
        except TimeoutError:
            _LOGGER.debug("Halfhour's device list didn't follow the change within %s s; it will arrive later", LIST_WAIT)
        finally:
            unsub()


def _newer_or_same(held: int | None, written: Any) -> bool:
    """Whether the held list's revision has reached the one a write returned (unknown counts as reached)."""
    if not isinstance(written, int) or isinstance(written, bool):
        return True
    return held is not None and held >= written
