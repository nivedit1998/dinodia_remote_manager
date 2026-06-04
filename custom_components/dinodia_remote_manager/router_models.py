"""Shared router dataclasses for Dinodia Remote Manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class RemoteEvent:
    """Normalized incoming remote event."""

    source: str
    remote_device_id: str
    action: str
    subtype: str | None = None
    command: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "remote_device_id": self.remote_device_id,
            "action": self.action,
            "subtype": self.subtype,
            "command": self.command,
            "payload": self.payload,
        }


@dataclass(slots=True, frozen=True)
class RouteResult:
    """Structured routing result."""

    routed: bool
    remote_device_id: str
    binding_id: str | None
    target_kind: str
    target_device_id: str | None
    target_entity_id: str | None
    domain: str
    service: str
    action: str
    source: str
    reason: str | None = None
    service_data: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "routed": self.routed,
            "remote_device_id": self.remote_device_id,
            "binding_id": self.binding_id,
            "target_kind": self.target_kind,
            "target_device_id": self.target_device_id,
            "target_entity_id": self.target_entity_id,
            "domain": self.domain,
            "service": self.service,
            "action": self.action,
            "source": self.source,
            "reason": self.reason,
            "service_data": self.service_data or {},
        }
