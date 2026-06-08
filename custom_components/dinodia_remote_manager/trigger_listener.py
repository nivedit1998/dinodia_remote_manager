"""Listener wiring for remote trigger events."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import (
    ATTR_HANDLED_BY_SERVICE,
    DOMAIN,
    EVENT_REMOTE_MANAGER,
)
from .capabilities import async_device_has_triggers, _registry_looks_remote_like
from .remote_router import RemoteRouter


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
            remote_router.async_handle_normalized_event(
                event_data,
                source=str(event_data.get("source") or EVENT_REMOTE_MANAGER),
            )
        )

    unsubscribers.append(hass.bus.async_listen("zha_event", _handle_zha_event))
    unsubscribers.append(hass.bus.async_listen(EVENT_REMOTE_MANAGER, _handle_internal_remote_event))
    return unsubscribers
