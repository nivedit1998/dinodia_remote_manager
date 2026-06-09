"""Target capability routing for Dinodia Remote Manager."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, State

from .capabilities import ResolvedCapability, async_resolve_target_capability
from .router_models import RemoteEvent, RouteResult
from .store import RemoteBinding

LOGGER = logging.getLogger(__name__)

class EventRouter:
    """Route remote actions to Home Assistant services."""

    def __init__(self, hass: HomeAssistant, store) -> None:
        self.hass = hass
        self._store = store

    def no_binding(self, remote_event: RemoteEvent) -> RouteResult:
        return RouteResult(
            routed=False,
            remote_device_id=remote_event.remote_device_id,
            binding_id=None,
            target_kind="unknown",
            target_device_id=None,
            target_entity_id=None,
            domain="",
            service="",
            action=remote_event.action,
            source=remote_event.source,
            reason="No binding found for remote device.",
        )

    async def async_route_binding(self, binding: RemoteBinding, remote_event: RemoteEvent) -> RouteResult:
        if not binding.enabled:
            return RouteResult(
                routed=False,
                remote_device_id=remote_event.remote_device_id,
                binding_id=binding.binding_id,
                target_kind=binding.target_kind,
                target_device_id=binding.target_device_id,
                target_entity_id=binding.target_entity_id,
                domain="",
                service="",
                action=remote_event.action,
                source=remote_event.source,
                reason="Binding is disabled.",
            )

        capability = await async_resolve_target_capability(
            self.hass,
            target_device_id=binding.target_device_id,
            target_entity_id=binding.target_entity_id,
        )
        if not capability.supported:
            return self._unsupported(capability, binding, remote_event)

        route = self._resolve_route(capability, remote_event)
        if route is None:
            LOGGER.debug(
                "No route for remote event",
                extra={
                    "remote_device_id": remote_event.remote_device_id,
                    "binding_id": binding.binding_id,
                    "target_kind": capability.target_kind,
                    "action": remote_event.action,
                },
            )
            return RouteResult(
                routed=False,
                remote_device_id=remote_event.remote_device_id,
                binding_id=binding.binding_id,
                target_kind=capability.target_kind,
                target_device_id=capability.target_device_id,
                target_entity_id=capability.target_entity_id,
                domain="",
                service="",
                action=remote_event.action,
                source=remote_event.source,
                reason=f"No service mapping exists for action '{remote_event.action}' on target kind '{capability.target_kind}'.",
            )

        domain, service, service_data = route
        target_entity_id = capability.target_entity_id
        if target_entity_id is None:
            return RouteResult(
                routed=False,
                remote_device_id=remote_event.remote_device_id,
                binding_id=binding.binding_id,
                target_kind=capability.target_kind,
                target_device_id=capability.target_device_id,
                target_entity_id=None,
                domain=domain,
                service=service,
                action=remote_event.action,
                source=remote_event.source,
                reason="Resolved target entity is missing.",
                service_data=service_data,
            )

        await self.hass.services.async_call(
            domain,
            service,
            {**service_data, "entity_id": target_entity_id},
            blocking=True,
        )
        LOGGER.debug(
            "Routed remote event",
            extra={
                "remote_device_id": remote_event.remote_device_id,
                "binding_id": binding.binding_id,
                "target_kind": capability.target_kind,
                "target_entity_id": target_entity_id,
                "domain": domain,
                "service": service,
                "action": remote_event.action,
            },
        )
        return RouteResult(
            routed=True,
            remote_device_id=remote_event.remote_device_id,
            binding_id=binding.binding_id,
            target_kind=capability.target_kind,
            target_device_id=capability.target_device_id,
            target_entity_id=target_entity_id,
            domain=domain,
            service=service,
            action=remote_event.action,
            source=remote_event.source,
            service_data={**service_data, "entity_id": target_entity_id},
        )

    def _unsupported(
        self,
        capability: ResolvedCapability,
        binding: RemoteBinding,
        remote_event: RemoteEvent,
    ) -> RouteResult:
        return RouteResult(
            routed=False,
            remote_device_id=remote_event.remote_device_id,
            binding_id=binding.binding_id,
            target_kind=capability.target_kind,
            target_device_id=capability.target_device_id,
            target_entity_id=capability.target_entity_id,
            domain=capability.domain,
            service="",
            action=remote_event.action,
            source=remote_event.source,
            reason=capability.reason or "Unsupported target.",
        )

    def _resolve_route(
        self,
        capability: ResolvedCapability,
        remote_event: RemoteEvent,
    ) -> tuple[str, str, dict[str, Any]] | None:
        kind = capability.target_kind
        action = remote_event.action
        payload = remote_event.payload

        if kind in {"light", "switch"}:
            return self._resolve_light_or_switch(kind, action, payload)
        if kind == "cover":
            return self._resolve_cover(action, payload, capability.target_entity_id)
        if kind == "climate":
            return self._resolve_climate(action, payload, capability.target_entity_id)
        if kind == "media_player":
            return self._resolve_media_player(action, payload)
        if kind == "fan":
            return self._resolve_fan(action, payload, capability.target_entity_id)
        if kind == "lock":
            return self._resolve_lock(action, capability.target_entity_id)
        if kind == "vacuum":
            return self._resolve_vacuum(action, capability.target_entity_id)
        if kind == "humidifier":
            return self._resolve_humidifier(action, payload, capability.target_entity_id)
        return None

    def _resolve_light_or_switch(
        self,
        kind: str,
        action: str,
        payload: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]] | None:
        if action in {"turn_on", "on"}:
            return kind, "turn_on", {}
        if action in {"turn_off", "off"}:
            return kind, "turn_off", {}
        if action in {"toggle", "short_press"}:
            return kind, "toggle", {}
        if action in {"increase", "brightness", "brightness_up"} and kind == "light":
            return kind, "turn_on", {"brightness_step_pct": self._step_value(payload, 10)}
        if action in {"decrease", "brightness_down"} and kind == "light":
            return kind, "turn_on", {"brightness_step_pct": -abs(self._step_value(payload, 10))}
        return None

    def _resolve_cover(
        self,
        action: str,
        payload: dict[str, Any],
        target_entity_id: str | None,
    ) -> tuple[str, str, dict[str, Any]] | None:
        if action in {"open", "turn_on"}:
            return "cover", "open_cover", {}
        if action in {"close", "turn_off"}:
            return "cover", "close_cover", {}
        if action in {"stop"}:
            return "cover", "stop_cover", {}
        if action in {"toggle"}:
            if target_entity_id is None:
                return "cover", "open_cover", {}
            state = self.hass.states.get(target_entity_id)
            if state is not None and state.state == "open":
                return "cover", "close_cover", {}
            return "cover", "open_cover", {}
        if action in {"increase", "decrease", "position"}:
            position = payload.get("position")
            if position is not None:
                return "cover", "set_cover_position", {"position": position}
        return None

    def _resolve_climate(
        self,
        action: str,
        payload: dict[str, Any],
        target_entity_id: str | None,
    ) -> tuple[str, str, dict[str, Any]] | None:
        if action in {"turn_on"}:
            return "climate", "turn_on", {}
        if action in {"turn_off"}:
            return "climate", "turn_off", {}
        if action in {"toggle"}:
            if target_entity_id is None:
                return None
            state = self.hass.states.get(target_entity_id)
            if state is not None and state.state == "off":
                return "climate", "turn_on", {}
            return "climate", "turn_off", {}
        if action in {"increase", "temperature_up", "up"}:
            return self._set_temperature_step(target_entity_id, payload, 1.0)
        if action in {"decrease", "temperature_down", "down"}:
            return self._set_temperature_step(target_entity_id, payload, -1.0)
        if action in {"temperature_set"}:
            temperature = payload.get("temperature")
            if temperature is not None:
                return "climate", "set_temperature", {"temperature": temperature}
        return None

    def _resolve_media_player(self, action: str, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
        if action in {"turn_on"}:
            return "media_player", "turn_on", {}
        if action in {"turn_off"}:
            return "media_player", "turn_off", {}
        if action in {"toggle", "play_pause"}:
            return "media_player", "media_play_pause", {}
        if action in {"increase", "volume_up"}:
            return "media_player", "volume_up", {}
        if action in {"decrease", "volume_down"}:
            return "media_player", "volume_down", {}
        if action in {"play"}:
            return "media_player", "media_play", {}
        if action in {"pause"}:
            return "media_player", "media_pause", {}
        if action in {"next"}:
            return "media_player", "media_next_track", {}
        if action in {"previous"}:
            return "media_player", "media_previous_track", {}
        return None

    def _resolve_fan(
        self,
        action: str,
        payload: dict[str, Any],
        target_entity_id: str | None,
    ) -> tuple[str, str, dict[str, Any]] | None:
        if action in {"turn_on", "on", "increase"}:
            return "fan", "turn_on", {}
        if action in {"turn_off", "off"}:
            return "fan", "turn_off", {}
        if action in {"toggle", "short_press"}:
            return "fan", "toggle", {}
        if action in {"decrease"}:
            if target_entity_id is None:
                return "fan", "turn_off", {}
            state = self.hass.states.get(target_entity_id)
            percentage = state.attributes.get("percentage") if state is not None else None
            if isinstance(percentage, (int, float)) and percentage > 0:
                return "fan", "set_percentage", {"percentage": max(0, int(percentage) - int(self._step_value(payload, 10)))}
            return "fan", "turn_off", {}
        return None

    def _resolve_lock(
        self,
        action: str,
        target_entity_id: str | None,
    ) -> tuple[str, str, dict[str, Any]] | None:
        if action in {"lock", "turn_off", "off", "close"}:
            return "lock", "lock", {}
        if action in {"unlock", "turn_on", "on", "open"}:
            return "lock", "unlock", {}
        if action in {"toggle", "short_press"}:
            if target_entity_id is None:
                return "lock", "lock", {}
            state = self.hass.states.get(target_entity_id)
            if state is not None and state.state == "locked":
                return "lock", "unlock", {}
            return "lock", "lock", {}
        return None

    def _resolve_vacuum(
        self,
        action: str,
        target_entity_id: str | None,
    ) -> tuple[str, str, dict[str, Any]] | None:
        if action in {"turn_on", "on", "start", "toggle", "short_press"}:
            if target_entity_id is not None:
                state = self.hass.states.get(target_entity_id)
                if state is not None and state.state in {"cleaning", "returning"} and action in {"toggle", "short_press"}:
                    return "vacuum", "pause", {}
            return "vacuum", "start", {}
        if action in {"turn_off", "off", "stop"}:
            return "vacuum", "stop", {}
        if action in {"pause"}:
            return "vacuum", "pause", {}
        if action in {"return_to_base", "close"}:
            return "vacuum", "return_to_base", {}
        return None

    def _resolve_humidifier(
        self,
        action: str,
        payload: dict[str, Any],
        target_entity_id: str | None,
    ) -> tuple[str, str, dict[str, Any]] | None:
        if action in {"turn_on", "on"}:
            return "humidifier", "turn_on", {}
        if action in {"turn_off", "off"}:
            return "humidifier", "turn_off", {}
        if action in {"toggle", "short_press"}:
            if target_entity_id is None:
                return "humidifier", "turn_on", {}
            state = self.hass.states.get(target_entity_id)
            return "humidifier", "turn_off" if state is not None and state.state == "on" else "turn_on", {}
        if action in {"increase", "humidity_up", "up", "decrease", "humidity_down", "down"}:
            if target_entity_id is None:
                return None
            state = self.hass.states.get(target_entity_id)
            current = state.attributes.get("humidity") if state is not None else None
            if not isinstance(current, (int, float)):
                return None
            step = self._step_value(payload, 5)
            value = float(current) + (step if action in {"increase", "humidity_up", "up"} else -abs(step))
            return "humidifier", "set_humidity", {"humidity": max(0, min(100, round(value)))}
        return None

    def _set_temperature_step(
        self,
        target_entity_id: str | None,
        payload: dict[str, Any],
        delta: float,
    ) -> tuple[str, str, dict[str, Any]] | None:
        if target_entity_id is None:
            return None
        state: State | None = self.hass.states.get(target_entity_id)
        if state is None:
            return None
        current = state.attributes.get("temperature")
        if current is None:
            current = state.attributes.get("target_temp_low") or state.attributes.get("current_temperature")
        if current is None:
            return None
        step = self._step_value(payload, abs(delta))
        temperature = float(current) + (step if delta > 0 else -abs(step))
        return "climate", "set_temperature", {"temperature": temperature}

    @staticmethod
    def _step_value(payload: dict[str, Any], default: float) -> float:
        for key in ("step", "step_pct", "brightness_step_pct", "temperature_step", "delta"):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return float(default)
