"""Dinodia Remote Manager integration."""

from __future__ import annotations

from collections.abc import Callable

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError

from .binding_rules import resolve_action_profile
from .capabilities import async_resolve_target_capability
from .const import (
    ATTR_BINDING_ID,
    ATTR_REMOTE_DEVICE_ID,
    ATTR_TARGET_DEVICE_ID,
    ATTR_TARGET_ENTITY_ID,
    ATTR_TARGET_KIND,
    CONF_BINDING_NAME,
    CONF_ENABLED,
    DOMAIN,
    SERVICE_REGISTER_BINDING,
    SERVICE_RESOLVE_BINDING,
    SERVICE_UNBIND,
)
from .store import RemoteBinding, RemoteBindingStore

PLATFORMS: tuple[str, ...] = ()

REGISTER_BINDING_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_REMOTE_DEVICE_ID): str,
        vol.Optional(ATTR_TARGET_DEVICE_ID): str,
        vol.Optional(ATTR_TARGET_ENTITY_ID): str,
        vol.Optional(CONF_BINDING_NAME): str,
    }
)

UNBIND_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_BINDING_ID): str,
        vol.Optional(ATTR_REMOTE_DEVICE_ID): str,
    }
)

RESOLVE_BINDING_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_BINDING_ID): str,
        vol.Optional(ATTR_REMOTE_DEVICE_ID): str,
    }
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration."""
    del config
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry."""
    data = hass.data.setdefault(DOMAIN, {})
    store = data.get("store")
    if store is None:
        store = RemoteBindingStore(hass)
        await store.async_load()
        data["store"] = store

    binding = _binding_from_entry(entry)
    await store.async_upsert_binding(binding)
    data.setdefault("entries", set()).add(entry.entry_id)
    _register_services_once(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data.get(DOMAIN, {})
    store: RemoteBindingStore | None = data.get("store")
    if store is not None:
        binding_id = str(entry.data.get(ATTR_BINDING_ID) or "").strip()
        if binding_id:
            await store.async_remove_binding(binding_id=binding_id)
        entries = data.get("entries")
        if isinstance(entries, set):
            entries.discard(entry.entry_id)
    return True


@callback
def _binding_from_entry(entry: ConfigEntry) -> RemoteBinding:
    data = entry.data
    binding_id = str(data.get(ATTR_BINDING_ID) or entry.entry_id).strip()
    remote_device_id = str(data.get(ATTR_REMOTE_DEVICE_ID) or "").strip()
    target_device_id = str(data.get(ATTR_TARGET_DEVICE_ID) or "").strip() or None
    target_entity_id = str(data.get(ATTR_TARGET_ENTITY_ID) or "").strip() or None
    target_kind = str(data.get(ATTR_TARGET_KIND) or "").strip() or "unknown"
    binding_name = str(data.get(CONF_BINDING_NAME) or data.get(CONF_NAME) or "").strip() or None
    enabled = bool(data.get(CONF_ENABLED, True))
    return RemoteBinding(
        binding_id=binding_id,
        remote_device_id=remote_device_id,
        target_device_id=target_device_id,
        target_entity_id=target_entity_id,
        target_kind=target_kind,
        binding_name=binding_name,
        enabled=enabled,
    )


def _get_store(hass: HomeAssistant) -> RemoteBindingStore:
    data = hass.data.setdefault(DOMAIN, {})
    store = data.get("store")
    if store is None:
        store = RemoteBindingStore(hass)
        data["store"] = store
    return store


def _register_services_once(hass: HomeAssistant) -> None:
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("services_registered"):
        return

    async def handle_register_binding(call: ServiceCall):
        store = _get_store(hass)
        remote_device_id = str(call.data[ATTR_REMOTE_DEVICE_ID]).strip()
        target_device_id = str(call.data.get(ATTR_TARGET_DEVICE_ID) or "").strip() or None
        target_entity_id = str(call.data.get(ATTR_TARGET_ENTITY_ID) or "").strip() or None
        binding_name = str(call.data.get(CONF_BINDING_NAME) or "").strip() or None

        capability = await async_resolve_target_capability(
            hass,
            target_device_id=target_device_id,
            target_entity_id=target_entity_id,
        )
        if not capability.supported:
            raise HomeAssistantError(capability.reason or "Unsupported target")

        binding = RemoteBinding(
            binding_id=f"{remote_device_id}:{target_device_id or target_entity_id}",
            remote_device_id=remote_device_id,
            target_device_id=target_device_id,
            target_entity_id=target_entity_id,
            target_kind=capability.target_kind,
            binding_name=binding_name,
            enabled=True,
        )
        await store.async_upsert_binding(binding)
        return {"binding": binding.as_dict(), "capability": capability.as_dict()}

    async def handle_unbind(call: ServiceCall):
        store = _get_store(hass)
        binding_id = str(call.data.get(ATTR_BINDING_ID) or "").strip()
        remote_device_id = str(call.data.get(ATTR_REMOTE_DEVICE_ID) or "").strip()
        removed = await store.async_remove_binding(
            binding_id=binding_id or None,
            remote_device_id=remote_device_id or None,
        )
        return {"removed": removed}

    async def handle_resolve_binding(call: ServiceCall):
        store = _get_store(hass)
        binding = None
        binding_id = str(call.data.get(ATTR_BINDING_ID) or "").strip()
        remote_device_id = str(call.data.get(ATTR_REMOTE_DEVICE_ID) or "").strip()
        if binding_id:
            binding = store.async_get_binding(binding_id=binding_id)
        elif remote_device_id:
            binding = store.async_get_binding_by_remote(remote_device_id)
        if binding is None:
            raise HomeAssistantError("Binding not found")

        capability = await async_resolve_target_capability(
            hass,
            target_device_id=binding.target_device_id,
            target_entity_id=binding.target_entity_id,
        )
        return {"binding": binding.as_dict(), "capability": capability.as_dict()}

    hass.services.async_register(
        DOMAIN,
        SERVICE_REGISTER_BINDING,
        handle_register_binding,
        schema=REGISTER_BINDING_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UNBIND,
        handle_unbind,
        schema=UNBIND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESOLVE_BINDING,
        handle_resolve_binding,
        schema=RESOLVE_BINDING_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    data["services_registered"] = True

