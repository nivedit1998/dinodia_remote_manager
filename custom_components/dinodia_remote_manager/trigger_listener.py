"""Listener wiring for remote trigger events."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

try:
    from homeassistant.helpers.trigger import async_initialize_triggers
except ImportError:  # pragma: no cover - HA version compatibility
    async_initialize_triggers = None  # type: ignore[assignment]

from .const import (
    ATTR_HANDLED_BY_SERVICE,
    ATTR_EVENT_COMMAND,
    ATTR_EVENT_PAYLOAD,
    ATTR_EVENT_SOURCE,
    ATTR_EVENT_SUBTYPE,
    ATTR_EVENT_TYPE,
    ATTR_REMOTE_DEVICE_ID,
    DATA_RECENT_ROUTED_TRIGGER_EVENTS,
    DATA_RUNTIME_TRIGGER_UNSUBSCRIBERS,
    DOMAIN,
    EVENT_REMOTE_MANAGER,
)
from .capabilities import async_device_has_triggers, async_get_device_trigger_discovery, _registry_looks_remote_like
from .remote_router import RemoteRouter

DEDUP_WINDOW_SECONDS = 0.75


def _device_is_remote_like_fast(hass: HomeAssistant, device_id: str) -> bool:
    data = hass.data.get(DOMAIN, {})
    store = data.get("store")
    if store is not None and store.async_get_binding_by_remote(device_id) is not None:
        return True

    device_reg = dr.async_get(hass)
    device = device_reg.async_get(device_id)
    if device is None:
        return False
    labels = getattr(device, "labels", None) or []
    for label in labels:
        label_name = str(getattr(label, "name", label) or "").strip().lower()
        if "remote" in label_name:
            return True
    return False


def _build_normalized_payload(event_data: dict) -> dict[str, object]:
    device_id = str(event_data.get("device_id") or "").strip()
    return {
        "remote_device_id": device_id,
        "event_type": str(event_data.get("type") or event_data.get("command") or event_data.get("subtype") or "").strip(),
        "event_subtype": str(event_data.get("subtype") or "").strip() or None,
        "command": str(event_data.get("command") or "").strip() or None,
        "source": "zha_event",
        "payload": event_data,
    }


def _normalize_token(value: object | None) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _action_from_args(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    args = payload.get("args")
    values: list[Any] = []
    if isinstance(args, dict):
        values.extend(args.get(key) for key in ("move_mode", "step_mode", "direction"))
    elif isinstance(args, (list, tuple)):
        values.extend(args)
    values.extend(payload.get(key) for key in ("move_mode", "step_mode", "direction"))
    for value in values:
        token = _normalize_token(value)
        if token in {"0", "up", "increase", "inc", "positive", "forward"}:
            return "increase"
        if token in {"1", "down", "decrease", "dec", "negative", "backward"}:
            return "decrease"
    return ""


def _normalize_native_action(payload: dict[str, Any], trigger_config: dict[str, Any]) -> tuple[str, str]:
    candidates = (
        payload.get("command"),
        payload.get("action"),
        payload.get("event_type"),
        payload.get("type"),
        payload.get("subtype"),
        trigger_config.get("command"),
        trigger_config.get("type"),
        trigger_config.get("subtype"),
    )
    raw = ""
    for candidate in candidates:
        raw = _normalize_token(candidate)
        if raw:
            break
    arg_action = _action_from_args(payload) or _action_from_args(trigger_config)
    if arg_action:
        return arg_action, raw or arg_action
    aliases = {
        "pressed": "toggle",
        "press": "toggle",
        "short_press": "toggle",
        "single": "toggle",
        "single_press": "toggle",
        "click": "toggle",
        "double": "toggle",
        "double_press": "toggle",
        "long_press": "toggle",
        "hold": "toggle",
        "released": "stop",
        "release": "stop",
        "stop": "stop",
        "move_with_on_off": "increase",
        "move": "increase",
        "step_with_on_off": "increase",
        "step": "increase",
        "up": "increase",
        "down": "decrease",
        "on": "turn_on",
        "off": "turn_off",
        "open": "open",
        "close": "close",
    }
    return aliases.get(raw, raw or "unknown"), raw


def _recent_events(hass: HomeAssistant) -> dict[str, float]:
    data = hass.data.setdefault(DOMAIN, {})
    events = data.get(DATA_RECENT_ROUTED_TRIGGER_EVENTS)
    if not isinstance(events, dict):
        events = {}
        data[DATA_RECENT_ROUTED_TRIGGER_EVENTS] = events
    return events


def _dedup_key(event_data: dict[str, Any]) -> str:
    return "|".join(
        _normalize_token(event_data.get(key))
        for key in (ATTR_REMOTE_DEVICE_ID, ATTR_EVENT_TYPE, ATTR_EVENT_SUBTYPE, ATTR_EVENT_COMMAND)
    )


async def _route_event_once(
    hass: HomeAssistant,
    remote_router: RemoteRouter,
    event_data: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    now = time.monotonic()
    recent = _recent_events(hass)
    for key, timestamp in list(recent.items()):
        if now - timestamp > DEDUP_WINDOW_SECONDS:
            recent.pop(key, None)
    key = _dedup_key(event_data)
    if key and now - float(recent.get(key, 0)) <= DEDUP_WINDOW_SECONDS:
        result = {
            "routed": False,
            "deduped": True,
            "remote_device_id": event_data.get(ATTR_REMOTE_DEVICE_ID),
            "action": event_data.get(ATTR_EVENT_TYPE),
            "source": source,
            "reason": "duplicate_trigger_event",
        }
        hass.data.setdefault(DOMAIN, {})["last_route_result"] = result
        return result
    if key:
        recent[key] = now
    result = await remote_router.async_handle_normalized_event(event_data, source=source)
    payload = {**result.as_dict(), "deduped": False}
    hass.data.setdefault(DOMAIN, {})["last_route_result"] = payload
    return payload


async def _maybe_fire_trigger_device_event(hass: HomeAssistant, event_data: dict) -> None:
    device_id = str(event_data.get("device_id") or "").strip()
    if not device_id:
        return
    device_reg = dr.async_get(hass)
    device = device_reg.async_get(device_id)
    if device is None:
        return
    # Runtime routing may use remote-like hints after a real event fires.
    # Inventory classification is stricter and requires trigger evidence from capabilities.py.
    if not (await async_device_has_triggers(hass, device_id) or _registry_looks_remote_like(hass, device)):
        return
    hass.bus.async_fire(EVENT_REMOTE_MANAGER, _build_normalized_payload(event_data))


def async_start_trigger_listeners(hass: HomeAssistant, remote_router: RemoteRouter) -> list[Callable[[], None]]:
    """Start listeners for both ZHA events and internal remote-manager events."""
    unsubscribers: list[Callable[[], None]] = []

    @callback
    def _handle_zha_event(event: Event) -> None:
        event_data = dict(event.data or {})
        device_id = str(event_data.get("device_id") or "").strip()
        if not device_id:
            return
        if _device_is_remote_like_fast(hass, device_id):
            hass.bus.async_fire(EVENT_REMOTE_MANAGER, _build_normalized_payload(event_data))
            return
        hass.async_create_task(_maybe_fire_trigger_device_event(hass, event_data))

    @callback
    def _handle_internal_remote_event(event: Event) -> None:
        event_data = dict(event.data or {})
        if event_data.get(ATTR_HANDLED_BY_SERVICE):
            return
        hass.async_create_task(
            _route_event_once(
                hass,
                remote_router,
                event_data,
                source=str(event_data.get("source") or EVENT_REMOTE_MANAGER),
            )
        )

    unsubscribers.append(hass.bus.async_listen("zha_event", _handle_zha_event))
    unsubscribers.append(hass.bus.async_listen(EVENT_REMOTE_MANAGER, _handle_internal_remote_event))
    return unsubscribers


def _runtime_unsubscribers(hass: HomeAssistant) -> dict[str, list[Callable[[], None]]]:
    data = hass.data.setdefault(DOMAIN, {})
    value = data.get(DATA_RUNTIME_TRIGGER_UNSUBSCRIBERS)
    if not isinstance(value, dict):
        value = {}
        data[DATA_RUNTIME_TRIGGER_UNSUBSCRIBERS] = value
    return value


def async_stop_runtime_trigger_listener(hass: HomeAssistant, remote_device_id: str) -> int:
    device_id = str(remote_device_id or "").strip()
    unsubscribers = _runtime_unsubscribers(hass).pop(device_id, [])
    stopped = 0
    for unsubscribe in unsubscribers:
        try:
            unsubscribe()
            stopped += 1
        except Exception:  # pragma: no cover - defensive cleanup
            continue
    return stopped


def async_stop_all_runtime_trigger_listeners(hass: HomeAssistant) -> int:
    mapping = _runtime_unsubscribers(hass)
    total = 0
    for device_id in list(mapping.keys()):
        total += async_stop_runtime_trigger_listener(hass, device_id)
    return total


async def async_refresh_runtime_trigger_listener_for_binding(
    hass: HomeAssistant,
    remote_router: RemoteRouter,
    remote_device_id: str,
) -> dict[str, object]:
    device_id = str(remote_device_id or "").strip()
    if not device_id:
        return {"attached": False, "reason": "missing_remote_device_id", "triggerCount": 0}
    async_stop_runtime_trigger_listener(hass, device_id)
    if async_initialize_triggers is None:
        return {"attached": False, "reason": "native_trigger_helper_unavailable", "triggerCount": 0}
    try:
        discovery = await async_get_device_trigger_discovery(hass, device_id)
    except Exception as err:  # noqa: BLE001 - listener diagnostics must not break setup
        result = {
            "attached": False,
            "reason": f"trigger_discovery_error:{type(err).__name__}:{err}",
            "triggerCount": 0,
        }
        hass.data.setdefault(DOMAIN, {}).setdefault("listener_status", {})[device_id] = result
        return result
    trigger_configs = [dict(trigger) for trigger in discovery.triggers if isinstance(trigger, dict)]
    if not trigger_configs:
        return {"attached": False, "reason": "no_triggers", "triggerCount": 0}
    unsubscribers: list[Callable[[], None]] = []
    errors: list[str] = []

    async def _handle_native_trigger(variables: dict[str, Any], trigger_config: dict[str, Any]) -> None:
        trigger_payload = dict(variables.get("trigger") or variables or {})
        action, raw_action = _normalize_native_action(trigger_payload, trigger_config)
        event_data = {
            ATTR_REMOTE_DEVICE_ID: device_id,
            ATTR_EVENT_TYPE: action,
            ATTR_EVENT_SUBTYPE: str(trigger_payload.get("subtype") or trigger_config.get("subtype") or "").strip() or None,
            ATTR_EVENT_COMMAND: str(trigger_payload.get("command") or trigger_config.get("command") or raw_action or "").strip() or None,
            ATTR_EVENT_SOURCE: "ha_device_trigger",
            ATTR_EVENT_PAYLOAD: {
                "trigger": trigger_payload,
                "trigger_config": trigger_config,
                "rawAction": raw_action,
                "normalizedAction": action,
            },
        }
        hass.data.setdefault(DOMAIN, {})["last_raw_callback_payload"] = event_data[ATTR_EVENT_PAYLOAD]
        await _route_event_once(hass, remote_router, event_data, source="ha_device_trigger")

    for config in trigger_configs:
        async def _action(variables: dict[str, Any], context=None, cfg=config) -> None:  # noqa: ANN001
            await _handle_native_trigger(dict(variables or {}), cfg)

        try:
            unsubscribe = await async_initialize_triggers(
                hass,
                [config],
                _action,
                DOMAIN,
                f"Dinodia trigger {device_id}",
                None,
            )
            if callable(unsubscribe):
                unsubscribers.append(unsubscribe)
        except TypeError:
            try:
                unsubscribe = await async_initialize_triggers(hass, [config], _action, DOMAIN, f"Dinodia trigger {device_id}")
                if callable(unsubscribe):
                    unsubscribers.append(unsubscribe)
            except Exception as err:  # noqa: BLE001
                errors.append(f"native_trigger_attach_error:{type(err).__name__}:{err}")
        except Exception as err:  # noqa: BLE001
            errors.append(f"native_trigger_attach_error:{type(err).__name__}:{err}")

    _runtime_unsubscribers(hass)[device_id] = unsubscribers
    result = {
        "attached": bool(unsubscribers),
        "reason": None if unsubscribers else (errors[0] if errors else "no_unsubscribers"),
        "triggerCount": len(trigger_configs),
        "attachedCount": len(unsubscribers),
        "errors": errors,
        "sources": list(discovery.sources),
    }
    hass.data.setdefault(DOMAIN, {}).setdefault("listener_status", {})[device_id] = result
    return result


async def async_refresh_runtime_trigger_listeners_for_all_bindings(
    hass: HomeAssistant,
    remote_router: RemoteRouter,
) -> dict[str, object]:
    data = hass.data.get(DOMAIN, {})
    store = data.get("store")
    if store is None or not hasattr(store, "async_list_bindings"):
        return {"attached": 0, "bindings": 0, "errors": ["store_unavailable"]}
    attached = 0
    errors: list[str] = []
    bindings = store.async_list_bindings()
    for binding in bindings:
        try:
            result = await async_refresh_runtime_trigger_listener_for_binding(hass, remote_router, binding.remote_device_id)
            if result.get("attached"):
                attached += 1
        except Exception as err:  # noqa: BLE001
            errors.append(f"{binding.remote_device_id}:{type(err).__name__}:{err}")
    return {"attached": attached, "bindings": len(bindings), "errors": errors}
