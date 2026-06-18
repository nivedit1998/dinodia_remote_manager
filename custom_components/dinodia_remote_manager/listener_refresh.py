"""Safe runtime trigger-listener refresh helpers."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .const import DATA_REMOTE_ROUTER, DOMAIN
from .trigger_listener import async_refresh_runtime_trigger_listener_for_binding

_LOGGER = logging.getLogger(__name__)


async def async_refresh_binding_listener_safe(
    hass: HomeAssistant,
    remote_device_id: str,
) -> dict[str, object]:
    """Refresh a binding listener without failing config entry setup."""
    device_id = str(remote_device_id or "").strip()
    data = hass.data.setdefault(DOMAIN, {})
    listener_status = data.setdefault("listener_status", {})
    if not isinstance(listener_status, dict):
        listener_status = {}
        data["listener_status"] = listener_status

    if not device_id:
        result = {"attached": False, "reason": "missing_remote_device_id", "triggerCount": 0}
        listener_status[device_id] = result
        return result

    remote_router = data.get(DATA_REMOTE_ROUTER)
    if remote_router is None:
        result = {"attached": False, "reason": "remote_router_unavailable", "triggerCount": 0}
        listener_status[device_id] = result
        return result

    try:
        result = await async_refresh_runtime_trigger_listener_for_binding(hass, remote_router, device_id)
    except Exception as err:  # noqa: BLE001 - config entries must stay loaded
        _LOGGER.warning(
            "Dinodia Remote Manager binding saved but runtime trigger listener failed for %s: %s",
            device_id,
            err,
        )
        result = {
            "attached": False,
            "reason": f"listener_error:{type(err).__name__}:{err}",
            "triggerCount": 0,
        }

    listener_status[device_id] = result
    if not result.get("attached"):
        data["last_listener_error"] = {
            "remote_device_id": device_id,
            "error": result.get("reason"),
        }
    return result
