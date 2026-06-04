"""Dinodia Remote Manager integration."""

from __future__ import annotations

from collections.abc import Callable

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError

from .capabilities import async_resolve_target_capability
from .const import (
    ATTR_BINDING_ID,
    ATTR_EVENT_COMMAND,
    ATTR_EVENT_PAYLOAD,
    ATTR_EVENT_SOURCE,
    ATTR_EVENT_SUBTYPE,
    ATTR_EVENT_TYPE,
    ATTR_HANDLED_BY_SERVICE,
    ATTR_REMOTE_DEVICE_ID,
    ATTR_TARGET_DEVICE_ID,
    ATTR_TARGET_ENTITY_ID,
    ATTR_TARGET_KIND,
    CONF_BINDING_NAME,
    CONF_ENABLED,
    DATA_EVENT_ROUTER,
    DATA_REMOTE_ROUTER,
    DATA_TRIGGER_LISTENERS,
    DOMAIN,
    EVENT_REMOTE_MANAGER,
    SERVICE_REGISTER_BINDING,
    SERVICE_RESOLVE_BINDING,
    SERVICE_SIMULATE_REMOTE_EVENT,
    SERVICE_UNBIND,
)
from .event_router import EventRouter
from .remote_router import RemoteRouter
from .store import RemoteBinding, RemoteBindingStore
from .trigger_listener import async_start_trigger_listeners

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

SIMULATE_REMOTE_EVENT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_REMOTE_DEVICE_ID): str,
        vol.Required(ATTR_EVENT_TYPE): str,
        vol.Optional(ATTR_EVENT_SUBTYPE): str,
        vol.Optional(ATTR_EVENT_COMMAND): str,
        vol.Optional(ATTR_EVENT_SOURCE): str,
        vol.Optional(ATTR_EVENT_PAYLOAD): dict,
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

    event_router = data.get(DATA_EVENT_ROUTER)
    if event_router is None:
        event_router = EventRouter(hass, store)
        data[DATA_EVENT_ROUTER] = event_router

    remote_router = data.get(DATA_REMOTE_ROUTER)
    if remote_router is None:
        remote_router = RemoteRouter(hass, store, event_router)
        data[DATA_REMOTE_ROUTER] = remote_router

    binding = _binding_from_entry(entry)
    await store.async_upsert_binding(binding)
    data.setdefault("entries", set()).add(entry.entry_id)
    _register_services_once(hass)
    _ensure_listener_started(hass)
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
            if not entries:
                _stop_listeners(hass)
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

    async def handle_simulate_remote_event(call: ServiceCall):
        remote_router: RemoteRouter | None = hass.data.get(DOMAIN, {}).get(DATA_REMOTE_ROUTER)
        if remote_router is None:
            raise HomeAssistantError("Remote router is not ready")

        event_data = {
            ATTR_REMOTE_DEVICE_ID: str(call.data[ATTR_REMOTE_DEVICE_ID]).strip(),
            ATTR_EVENT_TYPE: str(call.data[ATTR_EVENT_TYPE]).strip(),
            ATTR_EVENT_SUBTYPE: str(call.data.get(ATTR_EVENT_SUBTYPE) or "").strip() or None,
            ATTR_EVENT_COMMAND: str(call.data.get(ATTR_EVENT_COMMAND) or "").strip() or None,
            ATTR_EVENT_SOURCE: str(call.data.get(ATTR_EVENT_SOURCE) or "simulate_remote_event").strip(),
            ATTR_EVENT_PAYLOAD: dict(call.data.get(ATTR_EVENT_PAYLOAD) or {}),
        }
        result = await remote_router.async_handle_normalized_event(
            event_data,
            source=event_data[ATTR_EVENT_SOURCE],
        )
        hass.bus.async_fire(
            EVENT_REMOTE_MANAGER,
            {
                **event_data,
                ATTR_HANDLED_BY_SERVICE: True,
                "result": result.as_dict(),
            },
        )
        return result.as_dict()

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
    hass.services.async_register(
        DOMAIN,
        SERVICE_SIMULATE_REMOTE_EVENT,
        handle_simulate_remote_event,
        schema=SIMULATE_REMOTE_EVENT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    data["services_registered"] = True


def _ensure_listener_started(hass: HomeAssistant) -> None:
    data = hass.data.setdefault(DOMAIN, {})
    if data.get(DATA_TRIGGER_LISTENERS):
        return

    remote_router: RemoteRouter | None = data.get(DATA_REMOTE_ROUTER)
    if remote_router is None:
        remote_router = RemoteRouter(hass, _get_store(hass), EventRouter(hass, _get_store(hass)))
        data[DATA_REMOTE_ROUTER] = remote_router

    unsubscribers = async_start_trigger_listeners(hass, remote_router)
    data[DATA_TRIGGER_LISTENERS] = unsubscribers

    @callback
    def _stop_on_shutdown(_event) -> None:
        _stop_listeners(hass)

    data.setdefault("shutdown_unsubscribe", hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_on_shutdown))


def _stop_listeners(hass: HomeAssistant) -> None:
    data = hass.data.get(DOMAIN, {})
    unsubscribers = data.pop(DATA_TRIGGER_LISTENERS, None)
    if isinstance(unsubscribers, (list, tuple)):
        for unsubscribe in unsubscribers:
            try:
                unsubscribe()
            except Exception:  # pragma: no cover - defensive teardown
                continue
    shutdown_unsubscribe: Callable[[], None] | None = data.pop("shutdown_unsubscribe", None)
    if callable(shutdown_unsubscribe):
        shutdown_unsubscribe()
