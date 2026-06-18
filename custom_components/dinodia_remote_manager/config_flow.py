"""Config flow for Dinodia Remote Manager."""

from __future__ import annotations

import uuid
from copy import deepcopy

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .capabilities import (
    async_get_remote_device_choices,
    async_get_supported_target_device_choices,
    async_get_supported_target_entity_choices,
    async_resolve_target_capability,
    clear_trigger_discovery_cache,
)
from .const import (
    ATTR_BINDING_ID,
    ATTR_REMOTE_DEVICE_ID,
    ATTR_TARGET_DEVICE_ID,
    ATTR_TARGET_ENTITY_ID,
    ATTR_TARGET_KIND,
    CONF_BINDING_NAME,
    CONF_ENABLED,
    DOMAIN,
)
from .listener_refresh import async_refresh_binding_listener_safe
from .store import RemoteBinding, RemoteBindingStore

BOOTSTRAP_UNIQUE_ID = "bootstrap"
BOOTSTRAP_ENTRY_KIND = "bootstrap"
BOOTSTRAP_TITLE = "Dinodia Remote Manager"
BOOTSTRAP_SOURCE = "dinodia_auto_bootstrap"
BOOTSTRAP_CREATED_BY = "dinodia_app"
BOOTSTRAP_MANAGED_BY_APP = True


class DinodiaRemoteManagerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._remote_device_id: str | None = None
        self._target_mode: str | None = None
        self._target_device_id: str | None = None
        self._target_entity_id: str | None = None
        self._binding_name: str | None = None

    def _has_bootstrap_entry(self) -> bool:
        for entry in self._async_current_entries():
            if str(entry.data.get("entry_kind") or "").strip() == BOOTSTRAP_ENTRY_KIND:
                return True
            if str(getattr(entry, "unique_id", "") or "").strip() == BOOTSTRAP_UNIQUE_ID:
                return True
        return False

    def _bootstrap_entry_data(self) -> dict:
        return {
            "entry_kind": BOOTSTRAP_ENTRY_KIND,
            "source": BOOTSTRAP_SOURCE,
            "created_by": BOOTSTRAP_CREATED_BY,
            "managed_by_dinodia_app": BOOTSTRAP_MANAGED_BY_APP,
        }

    async def _create_bootstrap_entry(self) -> FlowResult:
        if self._has_bootstrap_entry():
            return self.async_abort(reason="bootstrap_ready")
        await self.async_set_unique_id(BOOTSTRAP_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=BOOTSTRAP_TITLE,
            data=self._bootstrap_entry_data(),
        )

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            remote_device_id = str(user_input.get(ATTR_REMOTE_DEVICE_ID) or "").strip()
            if not remote_device_id:
                errors["base"] = "remote_required"
            else:
                self._remote_device_id = remote_device_id
                self._binding_name = str(user_input.get(CONF_BINDING_NAME) or "").strip() or None
                return await self.async_step_target_mode()

        remote_choices = await async_get_remote_device_choices(self.hass)
        if not remote_choices:
            return await self._create_bootstrap_entry()

        schema = vol.Schema(
            {
                vol.Required(ATTR_REMOTE_DEVICE_ID): vol.In(remote_choices),
                vol.Optional(CONF_BINDING_NAME): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_service(self, user_input: dict | None = None) -> FlowResult:
        data = dict(user_input or {})
        entry_kind = str(data.get("entry_kind") or "").strip()
        if entry_kind == BOOTSTRAP_ENTRY_KIND or bool(data.get("bootstrap")):
            return await self._create_bootstrap_entry()
        binding_id = str(data.get(ATTR_BINDING_ID) or "").strip()
        remote_device_id = str(data.get(ATTR_REMOTE_DEVICE_ID) or "").strip()
        if not binding_id or not remote_device_id:
            return self.async_abort(reason="missing_binding")
        await self.async_set_unique_id(f"remote:{remote_device_id}")
        self._abort_if_unique_id_configured(updates=data)
        title = str(data.get(CONF_BINDING_NAME) or f"{remote_device_id} control").strip()
        return self.async_create_entry(title=title, data=data)

    async def async_step_target_mode(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            target_mode = str(user_input.get("target_mode") or "").strip()
            if target_mode not in {"device", "entity"}:
                return self.async_show_form(
                    step_id="target_mode",
                    data_schema=vol.Schema({vol.Required("target_mode"): vol.In({"device": "Device", "entity": "Entity"})}),
                    errors={"base": "invalid_target_mode"},
                )
            self._target_mode = target_mode
            if target_mode == "device":
                return await self.async_step_target_device()
            return await self.async_step_target_entity()

        schema = vol.Schema(
            {
                vol.Required("target_mode"): vol.In({"device": "Device", "entity": "Entity"}),
            }
        )
        return self.async_show_form(step_id="target_mode", data_schema=schema)

    async def async_step_target_device(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        choices = await async_get_supported_target_device_choices(self.hass)
        if not choices:
            return self.async_abort(reason="no_supported_target_devices")

        if user_input is not None:
            target_device_id = str(user_input.get(ATTR_TARGET_DEVICE_ID) or "").strip()
            if not target_device_id:
                errors["base"] = "target_required"
            elif target_device_id == self._remote_device_id:
                errors["base"] = "remote_and_target_same"
            else:
                capability = await async_resolve_target_capability(
                    self.hass,
                    target_device_id=target_device_id,
                )
                if not capability.supported:
                    errors["base"] = "unsupported_target"
                else:
                    self._target_device_id = target_device_id
                    return await self._create_entry(capability.target_kind)

        schema = vol.Schema(
            {
                vol.Required(ATTR_TARGET_DEVICE_ID): vol.In(choices),
            }
        )
        return self.async_show_form(step_id="target_device", data_schema=schema, errors=errors)

    async def async_step_target_entity(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        choices = await async_get_supported_target_entity_choices(self.hass)
        if not choices:
            return self.async_abort(reason="no_supported_target_entities")

        if user_input is not None:
            target_entity_id = str(user_input.get(ATTR_TARGET_ENTITY_ID) or "").strip()
            if not target_entity_id:
                errors["base"] = "target_required"
            else:
                if target_entity_id == self._remote_device_id:
                    errors["base"] = "remote_and_target_same"
                else:
                    capability = await async_resolve_target_capability(
                        self.hass,
                        target_entity_id=target_entity_id,
                    )
                    if not capability.supported:
                        errors["base"] = "unsupported_target"
                    else:
                        self._target_entity_id = target_entity_id
                        return await self._create_entry(capability.target_kind)

        schema = vol.Schema(
            {
                vol.Required(ATTR_TARGET_ENTITY_ID): vol.In(choices),
            }
        )
        return self.async_show_form(step_id="target_entity", data_schema=schema, errors=errors)

    async def _create_entry(self, target_kind: str) -> FlowResult:
        assert self._remote_device_id is not None
        binding_id = str(uuid.uuid4())
        binding_name = self._binding_name or f"{self._remote_device_id} → {self._target_device_id or self._target_entity_id}"
        data = {
            ATTR_BINDING_ID: binding_id,
            ATTR_REMOTE_DEVICE_ID: self._remote_device_id,
            ATTR_TARGET_DEVICE_ID: self._target_device_id,
            ATTR_TARGET_ENTITY_ID: self._target_entity_id,
            ATTR_TARGET_KIND: target_kind,
            CONF_BINDING_NAME: binding_name,
            CONF_ENABLED: True,
            "source": "ha_config_flow",
            "created_by": "home_assistant_ui",
            "managed_by_dinodia_app": False,
        }
        await self.async_set_unique_id(binding_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=binding_name, data=data)

async def async_get_options_flow(config_entry: config_entries.ConfigEntry):
    return DinodiaRemoteManagerOptionsFlow(config_entry)


class DinodiaRemoteManagerOptionsFlow(config_entries.OptionsFlow):
    """Options flow for editing a binding."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry
        self._remote_device_id = config_entry.data.get(ATTR_REMOTE_DEVICE_ID)
        self._binding_name = config_entry.data.get(CONF_BINDING_NAME)

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if not self._remote_device_id:
            return self.async_abort(reason="bootstrap_options_not_supported")
        if user_input is not None:
            new_data = deepcopy(dict(self.config_entry.data))
            new_data[CONF_BINDING_NAME] = str(user_input.get(CONF_BINDING_NAME) or "").strip() or new_data.get(CONF_BINDING_NAME)
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            self._binding_name = new_data.get(CONF_BINDING_NAME)
            return await self.async_step_target_mode()
        schema = vol.Schema(
            {
                vol.Optional(CONF_BINDING_NAME, default=self._binding_name or ""): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_target_mode(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            self._binding_name = str(user_input.get(CONF_BINDING_NAME) or "").strip() or self._binding_name
            target_mode = str(user_input.get("target_mode") or "").strip()
            if target_mode == "device":
                return await self.async_step_target_device()
            if target_mode == "entity":
                return await self.async_step_target_entity()
        return self.async_show_form(
            step_id="target_mode",
            data_schema=vol.Schema({vol.Required("target_mode"): vol.In({"device": "Device", "entity": "Entity"})}),
        )

    async def async_step_target_device(self, user_input: dict | None = None) -> FlowResult:
        choices = await async_get_supported_target_device_choices(self.hass)
        if user_input is not None:
            target_device_id = str(user_input.get(ATTR_TARGET_DEVICE_ID) or "").strip()
            capability = await async_resolve_target_capability(self.hass, target_device_id=target_device_id)
            if capability.supported:
                new_data = deepcopy(dict(self.config_entry.data))
                new_data[ATTR_TARGET_DEVICE_ID] = target_device_id
                new_data[ATTR_TARGET_ENTITY_ID] = capability.target_entity_id
                new_data[ATTR_TARGET_KIND] = capability.target_kind
                new_data[CONF_BINDING_NAME] = self._binding_name or self.config_entry.title
                new_data.setdefault("source", "ha_config_flow")
                new_data.setdefault("created_by", "home_assistant_ui")
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
                await self._persist_binding(new_data)
                clear_trigger_discovery_cache(self.hass, str(new_data.get(ATTR_REMOTE_DEVICE_ID) or "").strip())
                await async_refresh_binding_listener_safe(self.hass, str(new_data.get(ATTR_REMOTE_DEVICE_ID) or "").strip())
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="target_device",
            data_schema=vol.Schema({vol.Required(ATTR_TARGET_DEVICE_ID): vol.In(choices)}),
        )

    async def async_step_target_entity(self, user_input: dict | None = None) -> FlowResult:
        choices = await async_get_supported_target_entity_choices(self.hass)
        if user_input is not None:
            target_entity_id = str(user_input.get(ATTR_TARGET_ENTITY_ID) or "").strip()
            capability = await async_resolve_target_capability(self.hass, target_entity_id=target_entity_id)
            if capability.supported:
                new_data = deepcopy(dict(self.config_entry.data))
                new_data[ATTR_TARGET_ENTITY_ID] = target_entity_id
                new_data[ATTR_TARGET_DEVICE_ID] = capability.target_device_id
                new_data[ATTR_TARGET_KIND] = capability.target_kind
                new_data[CONF_BINDING_NAME] = self._binding_name or self.config_entry.title
                new_data.setdefault("source", "ha_config_flow")
                new_data.setdefault("created_by", "home_assistant_ui")
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
                await self._persist_binding(new_data)
                clear_trigger_discovery_cache(self.hass, str(new_data.get(ATTR_REMOTE_DEVICE_ID) or "").strip())
                await async_refresh_binding_listener_safe(self.hass, str(new_data.get(ATTR_REMOTE_DEVICE_ID) or "").strip())
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="target_entity",
            data_schema=vol.Schema({vol.Required(ATTR_TARGET_ENTITY_ID): vol.In(choices)}),
        )

    async def _persist_binding(self, data: dict) -> RemoteBinding:
        store: RemoteBindingStore | None = self.hass.data.get(DOMAIN, {}).get("store")
        if store is None:
            store = RemoteBindingStore(self.hass)
            await store.async_load()
            self.hass.data.setdefault(DOMAIN, {})["store"] = store

        binding_id = str(data.get(ATTR_BINDING_ID) or getattr(self, "config_entry", None) and self.config_entry.entry_id or "").strip()
        return await store.async_replace_binding_for_remote(
            binding_id=binding_id or None,
            remote_device_id=str(data.get(ATTR_REMOTE_DEVICE_ID) or "").strip(),
            target_device_id=str(data.get(ATTR_TARGET_DEVICE_ID) or "").strip() or None,
            target_entity_id=str(data.get(ATTR_TARGET_ENTITY_ID) or "").strip() or None,
            target_kind=str(data.get(ATTR_TARGET_KIND) or "unknown").strip() or "unknown",
            binding_name=str(data.get(CONF_BINDING_NAME) or "").strip() or None,
            enabled=bool(data.get(CONF_ENABLED, True)),
            owner_user_id=str(data.get("owner_user_id") or "").strip() or None,
            source=str(data.get("source") or "config_flow").strip() or "config_flow",
        )
