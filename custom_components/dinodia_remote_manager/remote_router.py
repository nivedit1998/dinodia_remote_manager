"""Remote event normalization and binding lookup for Dinodia Remote Manager."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import ATTR_EVENT_COMMAND, ATTR_EVENT_PAYLOAD, ATTR_EVENT_SOURCE, ATTR_EVENT_SUBTYPE, ATTR_EVENT_TYPE, ATTR_REMOTE_DEVICE_ID
from .event_router import EventRouter
from .router_models import RemoteEvent
from .store import RemoteBindingStore

LOGGER = logging.getLogger(__name__)


def _normalize_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    token = token.replace(" ", "_").replace("-", "_")
    return token


def _normalize_action(event_data: dict[str, Any]) -> str:
    args_action = _action_from_args(event_data.get(ATTR_EVENT_PAYLOAD))
    command = _normalize_token(event_data.get(ATTR_EVENT_COMMAND))
    subtype = _normalize_token(event_data.get(ATTR_EVENT_SUBTYPE))
    if command in {"stop", "stop_on_off"}:
        action = "stop"
    elif command in {"turn_on", "turn_off", "on", "off", "open", "close", "toggle", "play_pause", "volume_up", "volume_down"}:
        action = command
    elif args_action:
        action = args_action
    elif subtype:
        action = subtype
    else:
        candidates = (
            event_data.get("action"),
            event_data.get(ATTR_EVENT_TYPE),
            event_data.get("event"),
            event_data.get("type"),
            command,
        )
        action = ""
        for candidate in candidates:
            action = _normalize_token(candidate)
            if action:
                break

    aliases = {
        "single": "toggle",
        "single_press": "toggle",
        "short_press": "toggle",
        "press": "toggle",
        "click": "toggle",
        "long_press": "toggle",
        "on": "turn_on",
        "off": "turn_off",
        "dim_up": "increase",
        "brightness_up": "increase",
        "volume_up": "increase",
        "temperature_up": "increase",
        "increase": "increase",
        "up": "increase",
        "dim_down": "decrease",
        "brightness_down": "decrease",
        "volume_down": "decrease",
        "temperature_down": "decrease",
        "decrease": "decrease",
        "down": "decrease",
        "play_pause": "toggle",
        "stop_on_off": "stop",
    }
    return aliases.get(action, action or "unknown")


def _action_from_args(payload: Any) -> str:
    """Extract a directional action from ZHA args when the command alone is ambiguous."""
    if not isinstance(payload, dict):
        return ""
    args = payload.get("args")
    if isinstance(args, dict):
        move_mode = args.get("move_mode") if "move_mode" in args else args.get("step_mode")
        return _direction_from_value(move_mode)
    if isinstance(args, (list, tuple)):
        for item in args:
            if isinstance(item, dict):
                move_mode = item.get("move_mode") if "move_mode" in item else item.get("step_mode")
                direction = _direction_from_value(move_mode)
                if direction:
                    return direction
            else:
                direction = _direction_from_value(item)
                if direction:
                    return direction
    for key in ("move_mode", "step_mode", "direction"):
        if key in payload:
            direction = _direction_from_value(payload.get(key))
            if direction:
                return direction
    return ""


def _direction_from_value(value: Any) -> str:
    normalized = _normalize_token(value)
    if normalized in {"0", "up", "increase", "inc", "positive", "forward"}:
        return "increase"
    if normalized in {"1", "down", "decrease", "dec", "negative", "backward"}:
        return "decrease"
    return ""
class RemoteRouter:
    """Resolve incoming remote events to bindings and action routers."""

    def __init__(self, hass: HomeAssistant, store: RemoteBindingStore, event_router: EventRouter) -> None:
        self.hass = hass
        self._store = store
        self._event_router = event_router

    def normalize_event(self, event_data: dict[str, Any], *, source: str) -> RemoteEvent:
        payload = dict(event_data)
        remote_device_id = str(payload.get(ATTR_REMOTE_DEVICE_ID) or payload.get("device_id") or "").strip()
        action = _normalize_action(payload)
        subtype = str(payload.get(ATTR_EVENT_SUBTYPE) or "").strip() or None
        command = str(payload.get(ATTR_EVENT_COMMAND) or "").strip() or None
        if not subtype and command:
            subtype = command
        return RemoteEvent(
            source=str(source or payload.get(ATTR_EVENT_SOURCE) or "unknown").strip(),
            remote_device_id=remote_device_id,
            action=action,
            subtype=subtype,
            command=command,
            payload=payload,
        )

    async def async_handle_normalized_event(
        self,
        event_data: dict[str, Any],
        *,
        source: str,
    ) -> RouteResult:
        """Handle a normalized event from the listener or a test service."""
        remote_event = self.normalize_event(event_data, source=source)
        binding = self._store.async_get_binding_by_remote(remote_event.remote_device_id)
        if binding is None:
            LOGGER.debug(
                "Ignoring remote event with no binding",
                extra={
                    "remote_device_id": remote_event.remote_device_id,
                    "source": remote_event.source,
                    "action": remote_event.action,
                },
            )
            return self._event_router.no_binding(remote_event)
        return await self._event_router.async_route_binding(binding, remote_event)
