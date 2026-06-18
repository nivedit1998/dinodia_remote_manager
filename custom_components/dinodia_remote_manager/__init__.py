"""Dinodia Remote Manager integration."""

from __future__ import annotations

import logging
from collections.abc import Callable

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError

from .capabilities import (
    async_get_trigger_device_diagnostics,
    async_get_trigger_device_inventory,
    async_resolve_target_capability,
    clear_trigger_discovery_cache,
)
from .const import (
    ATTR_BINDING_ID,
    ATTR_EVENT_COMMAND,
    ATTR_EVENT_PAYLOAD,
    ATTR_EVENT_SOURCE,
    ATTR_EVENT_SUBTYPE,
    ATTR_EVENT_TYPE,
    ATTR_HANDLED_BY_SERVICE,
    ATTR_REMOTE_DEVICE_ID,
    ATTR_REMOTE_ENTITY_ID,
    ATTR_TARGET_DEVICE_ID,
    ATTR_TARGET_ENTITY_ID,
    ATTR_TARGET_KIND,
    CONF_BINDING_NAME,
    CONF_DIAGNOSTICS_ONLY,
    CONF_ENABLED,
    DATA_EVENT_ROUTER,
    DATA_REMOTE_ROUTER,
    DATA_TRIGGER_LISTENERS,
    DATA_RUNTIME_TRIGGER_UNSUBSCRIBERS,
    DOMAIN,
    EVENT_REMOTE_MANAGER,
    SERVICE_REGISTER_BINDING,
    SERVICE_LIST_BINDINGS,
    SERVICE_LIST_TRIGGER_DEVICE_DIAGNOSTICS,
    SERVICE_LIST_TRIGGER_DEVICES,
    SERVICE_REMOVE_TENANT_BINDINGS,
    SERVICE_REMOVE_TRIGGER_BINDINGS_FOR_DEVICES,
    SERVICE_RESOLVE_BINDING,
    SERVICE_SET_TRIGGER_TARGET,
    SERVICE_SIMULATE_REMOTE_EVENT,
    SERVICE_UNBIND,
    SERVICE_UPDATE_BINDING,
)
from .event_router import EventRouter
from .listener_refresh import async_refresh_binding_listener_safe
from .remote_router import RemoteRouter
from .store import RemoteBinding, RemoteBindingStore
from .trigger_listener import (
    async_refresh_runtime_trigger_listeners_for_all_bindings,
    async_start_trigger_listeners,
    async_stop_all_runtime_trigger_listeners,
    async_stop_runtime_trigger_listener,
)

PLATFORMS: tuple[str, ...] = ()
_LOGGER = logging.getLogger(__name__)

REGISTER_BINDING_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_REMOTE_DEVICE_ID): str,
        vol.Optional(ATTR_TARGET_DEVICE_ID): str,
        vol.Optional(ATTR_TARGET_ENTITY_ID): str,
        vol.Optional(CONF_BINDING_NAME): str,
        vol.Optional("owner_user_id"): str,
    }
)

SET_TRIGGER_TARGET_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_BINDING_ID): str,
        vol.Required(ATTR_REMOTE_DEVICE_ID): str,
        vol.Optional(ATTR_TARGET_DEVICE_ID): str,
        vol.Optional(ATTR_TARGET_ENTITY_ID): str,
        vol.Optional(CONF_BINDING_NAME): str,
        vol.Optional("owner_user_id"): str,
        vol.Optional("create_config_entry", default=True): bool,
    }
)

UPDATE_BINDING_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_BINDING_ID): str,
        vol.Required(ATTR_REMOTE_DEVICE_ID): str,
        vol.Optional(ATTR_TARGET_DEVICE_ID): str,
        vol.Optional(ATTR_TARGET_ENTITY_ID): str,
        vol.Optional(CONF_BINDING_NAME): str,
        vol.Optional("owner_user_id"): str,
    }
)

UNBIND_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_BINDING_ID): str,
        vol.Optional(ATTR_REMOTE_DEVICE_ID): str,
    }
)

REMOVE_TENANT_BINDINGS_SCHEMA = vol.Schema({vol.Required("owner_user_id"): str})
REMOVE_TRIGGER_BINDINGS_FOR_DEVICES_SCHEMA = vol.Schema(
    {
        vol.Required("owner_user_id"): str,
        vol.Required("remote_device_ids"): list,
    }
)

RESOLVE_BINDING_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_BINDING_ID): str,
        vol.Optional(ATTR_REMOTE_DEVICE_ID): str,
        vol.Optional(ATTR_REMOTE_ENTITY_ID): str,
    }
)

LIST_BINDINGS_SCHEMA = vol.Schema({})
LIST_TRIGGER_DEVICES_SCHEMA = vol.Schema({})
LIST_TRIGGER_DEVICE_DIAGNOSTICS_SCHEMA = vol.Schema({})

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

    _register_services_once(hass)
    _ensure_listener_started(hass)
    await _async_cleanup_duplicate_entries(hass, store)
    try:
        await async_refresh_runtime_trigger_listeners_for_all_bindings(hass, remote_router)
    except Exception as err:  # noqa: BLE001 - integration must still load
        _LOGGER.warning("Dinodia Remote Manager listener restore failed: %s", err)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry."""
    try:
        return await _async_setup_entry_safe(hass, entry)
    except Exception as err:  # noqa: BLE001 - never let a binding entry break HA UI load
        _LOGGER.exception("Dinodia Remote Manager config entry setup failed but entry will stay loaded")
        data = hass.data.setdefault(DOMAIN, {})
        data.setdefault("entry_status", {})[entry.entry_id] = {
            "bindingId": str(entry.data.get(ATTR_BINDING_ID) or entry.entry_id).strip(),
            "remoteDeviceId": str(entry.data.get(ATTR_REMOTE_DEVICE_ID) or "").strip(),
            "loaded": True,
            "setupWarning": f"{type(err).__name__}:{err}",
        }
        return True


async def _async_setup_entry_safe(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry without the outer HA failure guard."""
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

    diagnostics_only = bool(entry.data.get(CONF_DIAGNOSTICS_ONLY, False))
    remote_device_id = str(entry.data.get(ATTR_REMOTE_DEVICE_ID) or "").strip()
    data.setdefault("entries", set()).add(entry.entry_id)
    _register_services_once(hass)
    _ensure_listener_started(hass)
    if diagnostics_only or not remote_device_id:
        data.setdefault("entry_status", {})[entry.entry_id] = {
            "loaded": True,
            "diagnosticsOnly": diagnostics_only,
        }
        return True

    try:
        entry_binding = _binding_from_entry(entry)
        binding = await store.async_replace_binding_for_remote(
            binding_id=entry_binding.binding_id,
            remote_device_id=entry_binding.remote_device_id,
            target_device_id=entry_binding.target_device_id,
            target_entity_id=entry_binding.target_entity_id,
            target_kind=entry_binding.target_kind,
            binding_name=entry_binding.binding_name,
            enabled=entry_binding.enabled,
            owner_user_id=entry_binding.owner_user_id,
            source=entry_binding.source,
        )
        cleanup = await _async_cleanup_duplicate_entries(hass, store, keep_entry_id=entry.entry_id)
        binding = await store.async_replace_binding_for_remote(
            binding_id=binding.binding_id,
            remote_device_id=binding.remote_device_id,
            target_device_id=binding.target_device_id,
            target_entity_id=binding.target_entity_id,
            target_kind=binding.target_kind,
            binding_name=binding.binding_name,
            enabled=binding.enabled,
            owner_user_id=binding.owner_user_id,
            source=binding.source,
        )
        clear_trigger_discovery_cache(hass, binding.remote_device_id)
        listener_result = await async_refresh_binding_listener_safe(hass, binding.remote_device_id)
        data.setdefault("entry_status", {})[entry.entry_id] = {
            "bindingId": binding.binding_id,
            "remoteDeviceId": binding.remote_device_id,
            "listener": listener_result,
            "loaded": True,
            "duplicateCleanup": cleanup,
        }
    except Exception as err:  # noqa: BLE001 - keep HA config entry loaded and expose diagnostics
        _LOGGER.exception("Dinodia Remote Manager entry setup failed but entry will stay loaded")
        data.setdefault("entry_status", {})[entry.entry_id] = {
            "bindingId": str(entry.data.get(ATTR_BINDING_ID) or entry.entry_id).strip(),
            "remoteDeviceId": remote_device_id,
            "loaded": True,
            "setupWarning": f"{type(err).__name__}:{err}",
        }
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data.get(DOMAIN, {})
    store: RemoteBindingStore | None = data.get("store")
    if store is not None:
        binding_id = str(entry.data.get(ATTR_BINDING_ID) or "").strip()
        if binding_id:
            binding = store.async_get_binding(binding_id)
            if binding is not None:
                async_stop_runtime_trigger_listener(hass, binding.remote_device_id)
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
        owner_user_id=str(data.get("owner_user_id") or "").strip() or None,
        source=str(data.get("source") or "config_entry").strip() or "config_entry",
    )


def _get_store(hass: HomeAssistant) -> RemoteBindingStore:
    data = hass.data.setdefault(DOMAIN, {})
    store = data.get("store")
    if store is None:
        store = RemoteBindingStore(hass)
        data["store"] = store
    return store


@callback
def _update_matching_config_entries(
    hass: HomeAssistant,
    *,
    binding_id: str,
    remote_device_id: str,
    target_device_id: str | None,
    target_entity_id: str | None,
    target_kind: str,
    binding_name: str | None,
    owner_user_id: str | None = None,
) -> None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        entry_binding_id = str(entry.data.get(ATTR_BINDING_ID) or entry.entry_id).strip()
        entry_remote_device_id = str(entry.data.get(ATTR_REMOTE_DEVICE_ID) or "").strip()
        if entry_binding_id != binding_id and entry_remote_device_id != remote_device_id:
            continue

        new_data = dict(entry.data)
        new_data[ATTR_BINDING_ID] = binding_id
        new_data[ATTR_REMOTE_DEVICE_ID] = remote_device_id
        new_data[ATTR_TARGET_DEVICE_ID] = target_device_id
        new_data[ATTR_TARGET_ENTITY_ID] = target_entity_id
        new_data[ATTR_TARGET_KIND] = target_kind
        if owner_user_id is not None:
            new_data["owner_user_id"] = owner_user_id
        new_data["managed_by_dinodia_app"] = bool(new_data.get("managed_by_dinodia_app", False))
        if binding_name:
            new_data[CONF_BINDING_NAME] = binding_name
        hass.config_entries.async_update_entry(entry, data=new_data)


def _find_matching_config_entries(
    hass: HomeAssistant,
    *,
    binding_id: str | None = None,
    remote_device_id: str | None = None,
    owner_user_id: str | None = None,
) -> list[ConfigEntry]:
    matches: list[ConfigEntry] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = entry.data
        if binding_id and str(data.get(ATTR_BINDING_ID) or entry.entry_id).strip() == binding_id:
            matches.append(entry)
            continue
        if remote_device_id and str(data.get(ATTR_REMOTE_DEVICE_ID) or "").strip() == remote_device_id:
            matches.append(entry)
            continue
        if owner_user_id and str(data.get("owner_user_id") or "").strip() == owner_user_id:
            matches.append(entry)
    return matches


async def _async_cleanup_duplicate_entries(
    hass: HomeAssistant,
    store: RemoteBindingStore,
    *,
    keep_entry_id: str | None = None,
) -> dict[str, object]:
    """Collapse duplicate config entries and store bindings to one per trigger device."""
    removed_entries = 0
    errors: list[str] = []

    entries_by_remote: dict[str, list[ConfigEntry]] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        remote_device_id = str(entry.data.get(ATTR_REMOTE_DEVICE_ID) or "").strip()
        if not remote_device_id:
            continue
        entries_by_remote.setdefault(remote_device_id, []).append(entry)

    for remote_device_id, entries in entries_by_remote.items():
        if len(entries) <= 1:
            continue
        keep_entry = next((entry for entry in entries if entry.entry_id == keep_entry_id), None)
        if keep_entry is None:
            existing_binding = store.async_get_binding_by_remote(remote_device_id)
            if existing_binding is not None:
                keep_entry = next(
                    (
                        entry
                        for entry in entries
                        if str(entry.data.get(ATTR_BINDING_ID) or entry.entry_id).strip()
                        == existing_binding.binding_id
                    ),
                    None,
                )
        if keep_entry is None:
            keep_entry = sorted(entries, key=lambda item: item.entry_id)[-1]

        for entry in entries:
            if entry.entry_id == keep_entry.entry_id:
                continue
            try:
                await hass.config_entries.async_remove(entry.entry_id)
                removed_entries += 1
            except Exception as err:  # noqa: BLE001
                errors.append(f"{entry.entry_id}:{type(err).__name__}:{err}")

    for binding in list(store.async_list_bindings()):
        if not binding.remote_device_id:
            continue
        matches = store.async_find_bindings_for_remote_or_binding(remote_device_id=binding.remote_device_id)
        if len(matches) <= 1:
            continue
        keep = sorted(matches, key=lambda item: item.updated_at or item.created_at or item.binding_id)[-1]
        for stale in matches:
            if stale.binding_id == keep.binding_id:
                continue
            await store.async_remove_binding(binding_id=stale.binding_id)

    return {"removedConfigEntries": removed_entries, "errors": errors}


async def _async_ensure_binding_config_entry(
    hass: HomeAssistant,
    *,
    binding: RemoteBinding,
    capability,
    binding_name: str | None,
    owner_user_id: str | None,
) -> dict[str, object]:
    title = binding.binding_name or binding_name or f"{binding.remote_device_id} control"
    entry_data = {
        ATTR_BINDING_ID: binding.binding_id,
        ATTR_REMOTE_DEVICE_ID: binding.remote_device_id,
        ATTR_TARGET_DEVICE_ID: binding.target_device_id,
        ATTR_TARGET_ENTITY_ID: binding.target_entity_id,
        ATTR_TARGET_KIND: binding.target_kind,
        CONF_BINDING_NAME: title,
        CONF_ENABLED: binding.enabled,
        "owner_user_id": owner_user_id or binding.owner_user_id,
        "source": binding.source or "dinodia_app",
        "created_by": "dinodia_app",
        "managed_by_dinodia_app": True,
    }
    existing = _find_matching_config_entries(
        hass,
        binding_id=binding.binding_id,
        remote_device_id=binding.remote_device_id,
    )
    if existing:
        entry = existing[0]
        new_data = dict(entry.data)
        new_data.update(entry_data)
        hass.config_entries.async_update_entry(entry, title=title, data=new_data)
        return {"created": False, "updated": True, "entryId": entry.entry_id, "error": None}

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "service"},
        data=entry_data,
    )
    entry_id = None
    result_entry = result.get("result") if isinstance(result, dict) else None
    if result_entry is not None:
        entry_id = getattr(result_entry, "entry_id", None)
    if not entry_id and isinstance(result, dict):
        entry_id = result.get("entry_id") or result.get("flow_id")
    if not entry_id:
        raise HomeAssistantError("Could not create Dinodia Remote Manager entry")
    return {"created": True, "updated": False, "entryId": entry_id, "error": None}


async def _async_remove_config_entries_for_bindings(
    hass: HomeAssistant,
    bindings: list[RemoteBinding],
) -> dict[str, object]:
    removed = 0
    errors: list[str] = []
    for binding in bindings:
        for entry in _find_matching_config_entries(
            hass,
            binding_id=binding.binding_id,
            remote_device_id=binding.remote_device_id,
        ):
            try:
                await hass.config_entries.async_remove(entry.entry_id)
                removed += 1
            except Exception as err:  # noqa: BLE001
                errors.append(f"{entry.entry_id}:{type(err).__name__}:{err}")
    return {"removed": removed, "errors": errors}


async def _async_set_trigger_target(
    hass: HomeAssistant,
    *,
    binding_id: str | None,
    remote_device_id: str,
    target_device_id: str | None,
    target_entity_id: str | None,
    binding_name: str | None,
    owner_user_id: str | None,
    create_config_entry: bool = True,
) -> dict[str, object]:
    store = _get_store(hass)
    remote_device_id = str(remote_device_id or "").strip()
    binding_id = str(binding_id or "").strip() or None
    target_device_id = str(target_device_id or "").strip() or None
    target_entity_id = str(target_entity_id or "").strip() or None
    owner_user_id = str(owner_user_id or "").strip() or None
    if not remote_device_id:
        raise HomeAssistantError("Trigger device is required")
    if not target_device_id and not target_entity_id:
        raise HomeAssistantError("Target device or entity is required")

    accepted_ids = {
        str(item.get("device_id") or "").strip()
        for item in await async_get_trigger_device_inventory(hass)
    }
    if remote_device_id not in accepted_ids:
        raise HomeAssistantError("Trigger device is not accepted by Dinodia Remote Manager")

    capability = await async_resolve_target_capability(
        hass,
        target_device_id=target_device_id,
        target_entity_id=target_entity_id,
    )
    if not capability.supported:
        raise HomeAssistantError(capability.reason or "Unsupported target")
    resolved_target_device_id = capability.target_device_id or target_device_id
    if resolved_target_device_id and resolved_target_device_id == remote_device_id:
        raise HomeAssistantError("Trigger device and target cannot be the same device")

    previous_bindings = store.async_find_bindings_for_remote_or_binding(
        binding_id=binding_id,
        remote_device_id=remote_device_id,
    )
    binding = await store.async_replace_binding_for_remote(
        binding_id=binding_id,
        remote_device_id=remote_device_id,
        target_device_id=capability.target_device_id or target_device_id,
        target_entity_id=capability.target_entity_id or target_entity_id,
        target_kind=capability.target_kind,
        binding_name=binding_name,
        enabled=True,
        owner_user_id=owner_user_id,
        source="dinodia_app" if owner_user_id else "service",
    )

    config_entry_result: dict[str, object] | None = None
    try:
        if create_config_entry:
            config_entry_result = await _async_ensure_binding_config_entry(
                hass,
                binding=binding,
                capability=capability,
                binding_name=binding_name,
                owner_user_id=owner_user_id,
            )
    except Exception as err:
        if previous_bindings:
            await store.async_restore_bindings(previous_bindings)
        else:
            await store.async_remove_binding(binding_id=binding.binding_id)
        raise HomeAssistantError("Could not create Dinodia Remote Manager entry") from err
    if create_config_entry and not (config_entry_result or {}).get("entryId"):
        if previous_bindings:
            await store.async_restore_bindings(previous_bindings)
        else:
            await store.async_remove_binding(binding_id=binding.binding_id)
        raise HomeAssistantError("Could not create Dinodia Remote Manager entry")

    cleanup_result = await _async_cleanup_duplicate_entries(hass, store)
    clear_trigger_discovery_cache(hass, remote_device_id)
    _ensure_listener_started(hass)
    listener_result = await async_refresh_binding_listener_safe(hass, remote_device_id)

    resolved = store.async_find_binding(remote_device_id=remote_device_id)
    if resolved is None:
        raise HomeAssistantError("Could not verify trigger binding")
    return {
        "ok": True,
        "binding": resolved.as_api_dict(),
        "capability": capability.as_api_dict(),
        "configEntry": config_entry_result,
        "listener": listener_result,
        "duplicateCleanup": cleanup_result,
        "verified": True,
    }


def _register_services_once(hass: HomeAssistant) -> None:
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("services_registered"):
        return

    def register_service_once(handler: Callable, service: str, schema: vol.Schema, supports_response: SupportsResponse) -> None:
        if hass.services.has_service(DOMAIN, service):
            return
        hass.services.async_register(
            DOMAIN,
            service,
            handler,
            schema=schema,
            supports_response=supports_response,
        )

    async def handle_register_binding(call: ServiceCall):
        return await _async_set_trigger_target(
            hass,
            binding_id=None,
            remote_device_id=str(call.data[ATTR_REMOTE_DEVICE_ID]).strip(),
            target_device_id=str(call.data.get(ATTR_TARGET_DEVICE_ID) or "").strip() or None,
            target_entity_id=str(call.data.get(ATTR_TARGET_ENTITY_ID) or "").strip() or None,
            binding_name=str(call.data.get(CONF_BINDING_NAME) or "").strip() or None,
            owner_user_id=str(call.data.get("owner_user_id") or "").strip() or None,
            create_config_entry=True,
        )

    async def handle_set_trigger_target(call: ServiceCall):
        return await _async_set_trigger_target(
            hass,
            binding_id=str(call.data.get(ATTR_BINDING_ID) or "").strip() or None,
            remote_device_id=str(call.data[ATTR_REMOTE_DEVICE_ID]).strip(),
            target_device_id=str(call.data.get(ATTR_TARGET_DEVICE_ID) or "").strip() or None,
            target_entity_id=str(call.data.get(ATTR_TARGET_ENTITY_ID) or "").strip() or None,
            binding_name=str(call.data.get(CONF_BINDING_NAME) or "").strip() or None,
            owner_user_id=str(call.data.get("owner_user_id") or "").strip() or None,
            create_config_entry=bool(call.data.get("create_config_entry", True)),
        )

    async def handle_update_binding(call: ServiceCall):
        return await _async_set_trigger_target(
            hass,
            binding_id=str(call.data.get(ATTR_BINDING_ID) or "").strip() or None,
            remote_device_id=str(call.data[ATTR_REMOTE_DEVICE_ID]).strip(),
            target_device_id=str(call.data.get(ATTR_TARGET_DEVICE_ID) or "").strip() or None,
            target_entity_id=str(call.data.get(ATTR_TARGET_ENTITY_ID) or "").strip() or None,
            binding_name=str(call.data.get(CONF_BINDING_NAME) or "").strip() or None,
            owner_user_id=str(call.data.get("owner_user_id") or "").strip() or None,
            create_config_entry=True,
        )

    async def handle_unbind(call: ServiceCall):
        store = _get_store(hass)
        binding_id = str(call.data.get(ATTR_BINDING_ID) or "").strip()
        remote_device_id = str(call.data.get(ATTR_REMOTE_DEVICE_ID) or "").strip()
        removed = await store.async_remove_binding(
            binding_id=binding_id or None,
            remote_device_id=remote_device_id or None,
        )
        if remote_device_id:
            async_stop_runtime_trigger_listener(hass, remote_device_id)
        return {"removed": removed}

    async def handle_remove_tenant_bindings(call: ServiceCall):
        store = _get_store(hass)
        owner_user_id = str(call.data.get("owner_user_id") or "").strip()
        if not owner_user_id:
            raise HomeAssistantError("Owner user id is required")
        removed_bindings = await store.async_remove_bindings_for_owner(owner_user_id)
        config_result = await _async_remove_config_entries_for_bindings(hass, removed_bindings)
        listeners = 0
        for binding in removed_bindings:
            listeners += async_stop_runtime_trigger_listener(hass, binding.remote_device_id)
        return {
            "removed": {
                "bindings": len(removed_bindings),
                "configEntries": config_result.get("removed", 0),
                "listeners": listeners,
            },
            "errors": config_result.get("errors", []),
        }

    async def handle_remove_trigger_bindings_for_devices(call: ServiceCall):
        store = _get_store(hass)
        owner_user_id = str(call.data.get("owner_user_id") or "").strip()
        remote_device_ids = [str(item).strip() for item in call.data.get("remote_device_ids") or [] if str(item).strip()]
        if not owner_user_id:
            raise HomeAssistantError("Owner user id is required")
        removed_bindings = await store.async_remove_bindings_for_owner_devices(owner_user_id, remote_device_ids)
        config_result = await _async_remove_config_entries_for_bindings(hass, removed_bindings)
        listeners = 0
        for binding in removed_bindings:
            listeners += async_stop_runtime_trigger_listener(hass, binding.remote_device_id)
        return {
            "removed": {
                "bindings": len(removed_bindings),
                "configEntries": config_result.get("removed", 0),
                "listeners": listeners,
            },
            "errors": config_result.get("errors", []),
        }

    async def handle_resolve_binding(call: ServiceCall):
        store = _get_store(hass)
        binding = None
        binding_id = str(call.data.get(ATTR_BINDING_ID) or "").strip()
        remote_device_id = str(call.data.get(ATTR_REMOTE_DEVICE_ID) or "").strip()
        remote_entity_id = str(call.data.get(ATTR_REMOTE_ENTITY_ID) or "").strip()
        binding = store.async_find_binding(
            binding_id=binding_id or None,
            remote_device_id=remote_device_id or None,
            remote_device_aliases=[remote_entity_id] if remote_entity_id else None,
        )
        if binding is None:
            return {
                "binding": None,
                "capability": None,
                "reason": "Binding not found",
                "binding_lookup": {
                    "binding_id": binding_id or None,
                    "remote_device_id": remote_device_id or None,
                    "remote_entity_id": remote_entity_id or None,
                },
            }

        capability = await async_resolve_target_capability(
            hass,
            target_device_id=binding.target_device_id,
            target_entity_id=binding.target_entity_id,
        )
        return {
            "binding": binding.as_api_dict(),
            "capability": capability.as_api_dict(),
            "binding_lookup": {
                "binding_id": binding_id or None,
                "remote_device_id": remote_device_id or None,
                "remote_entity_id": remote_entity_id or None,
            },
        }

    async def handle_list_bindings(call: ServiceCall):
        del call
        store = _get_store(hass)
        accepted_ids = {
            str(item.get("device_id") or "").strip()
            for item in await async_get_trigger_device_inventory(hass)
        }
        runtime_unsubs = hass.data.get(DOMAIN, {}).get(DATA_RUNTIME_TRIGGER_UNSUBSCRIBERS, {})
        listener_status = hass.data.get(DOMAIN, {}).get("listener_status", {})
        entry_status = hass.data.get(DOMAIN, {}).get("entry_status", {})
        last_listener_error = hass.data.get(DOMAIN, {}).get("last_listener_error")
        last_route = hass.data.get(DOMAIN, {}).get("last_route_result")
        last_raw = hass.data.get(DOMAIN, {}).get("last_raw_callback_payload")
        rows = []
        for binding in store.async_list_bindings():
            capability = await async_resolve_target_capability(
                hass,
                target_device_id=binding.target_device_id,
                target_entity_id=binding.target_entity_id,
            )
            entries = _find_matching_config_entries(
                hass,
                binding_id=binding.binding_id,
                remote_device_id=binding.remote_device_id,
            )
            entry = entries[0] if entries else None
            binding_listener_status = (
                listener_status.get(binding.remote_device_id)
                if isinstance(listener_status, dict)
                else None
            )
            active_listener = bool(runtime_unsubs.get(binding.remote_device_id))
            accepted = binding.remote_device_id in accepted_ids
            binding_usable = accepted and active_listener
            rows.append(
                {
                    "binding": binding.as_api_dict(),
                    "hasConfigEntry": bool(entries),
                    "configEntryId": entry.entry_id if entry else None,
                    "entryLoaded": True if entry else None,
                    "entryStatus": str(getattr(entry, "state", "")) if entry else None,
                    "entryStatusDetails": entry_status.get(entry.entry_id) if entry and isinstance(entry_status, dict) else None,
                    "capability": capability.as_api_dict(),
                    "acceptedTriggerDevice": accepted,
                    "listenerActive": active_listener,
                    "listener": binding_listener_status,
                    "bindingUsable": binding_usable,
                    "reason": None if binding_usable else ("listener_not_attached" if accepted else "trigger_device_not_accepted"),
                    "warning": None if active_listener else "listener_not_attached",
                    "lastListenerError": last_listener_error,
                    "lastRoute": last_route,
                    "lastRawCallbackPayload": last_raw,
                    "duplicateConfigEntry": len(entries) > 1,
                }
            )
        return {
            "bindings": rows,
        }

    async def handle_list_trigger_devices(call: ServiceCall):
        del call
        return {
            "trigger_devices": await async_get_trigger_device_inventory(hass),
        }

    async def handle_list_trigger_device_diagnostics(call: ServiceCall):
        del call
        store = _get_store(hass)
        runtime_unsubs = hass.data.get(DOMAIN, {}).get(DATA_RUNTIME_TRIGGER_UNSUBSCRIBERS, {})
        listener_status = hass.data.get(DOMAIN, {}).get("listener_status", {})
        last_listener_error = hass.data.get(DOMAIN, {}).get("last_listener_error")
        rows = []
        for row in await async_get_trigger_device_diagnostics(hass):
            device_id = str(row.get("device_id") or "").strip()
            bindings = store.async_find_bindings_for_remote_or_binding(remote_device_id=device_id)
            entries = _find_matching_config_entries(hass, remote_device_id=device_id)
            row = dict(row)
            row["binding_count"] = len(bindings)
            row["config_entry_count"] = len(entries)
            row["listener_active"] = bool(runtime_unsubs.get(device_id))
            row["listener"] = listener_status.get(device_id) if isinstance(listener_status, dict) else None
            row["last_listener_error"] = last_listener_error
            row["duplicate_config_entry"] = len(entries) > 1
            rows.append(row)
        return {
            "candidates": rows,
        }

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

    register_service_once(
        handle_register_binding,
        SERVICE_REGISTER_BINDING,
        REGISTER_BINDING_SCHEMA,
        SupportsResponse.OPTIONAL,
    )
    register_service_once(
        handle_set_trigger_target,
        SERVICE_SET_TRIGGER_TARGET,
        SET_TRIGGER_TARGET_SCHEMA,
        SupportsResponse.ONLY,
    )
    register_service_once(
        handle_update_binding,
        SERVICE_UPDATE_BINDING,
        UPDATE_BINDING_SCHEMA,
        SupportsResponse.OPTIONAL,
    )
    register_service_once(
        handle_remove_tenant_bindings,
        SERVICE_REMOVE_TENANT_BINDINGS,
        REMOVE_TENANT_BINDINGS_SCHEMA,
        SupportsResponse.ONLY,
    )
    register_service_once(
        handle_remove_trigger_bindings_for_devices,
        SERVICE_REMOVE_TRIGGER_BINDINGS_FOR_DEVICES,
        REMOVE_TRIGGER_BINDINGS_FOR_DEVICES_SCHEMA,
        SupportsResponse.ONLY,
    )
    register_service_once(
        handle_unbind,
        SERVICE_UNBIND,
        UNBIND_SCHEMA,
        SupportsResponse.OPTIONAL,
    )
    register_service_once(
        handle_resolve_binding,
        SERVICE_RESOLVE_BINDING,
        RESOLVE_BINDING_SCHEMA,
        SupportsResponse.ONLY,
    )
    register_service_once(
        handle_list_bindings,
        SERVICE_LIST_BINDINGS,
        LIST_BINDINGS_SCHEMA,
        SupportsResponse.ONLY,
    )
    register_service_once(
        handle_list_trigger_devices,
        SERVICE_LIST_TRIGGER_DEVICES,
        LIST_TRIGGER_DEVICES_SCHEMA,
        SupportsResponse.ONLY,
    )
    register_service_once(
        handle_list_trigger_device_diagnostics,
        SERVICE_LIST_TRIGGER_DEVICE_DIAGNOSTICS,
        LIST_TRIGGER_DEVICE_DIAGNOSTICS_SCHEMA,
        SupportsResponse.ONLY,
    )
    register_service_once(
        handle_simulate_remote_event,
        SERVICE_SIMULATE_REMOTE_EVENT,
        SIMULATE_REMOTE_EVENT_SCHEMA,
        SupportsResponse.OPTIONAL,
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
    async_stop_all_runtime_trigger_listeners(hass)
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
