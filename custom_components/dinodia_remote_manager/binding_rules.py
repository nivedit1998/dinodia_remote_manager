"""Binding and capability rules for Dinodia Remote Manager."""

from __future__ import annotations

from dataclasses import dataclass

from .const import SUPPORTED_ACTIONABLE_TARGET_DOMAINS


@dataclass(frozen=True, slots=True)
class ActionProfile:
    """Normalized action profile for a target."""

    target_kind: str
    domain: str
    supported: bool
    actions: tuple[str, ...]
    description: str
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "target_kind": self.target_kind,
            "domain": self.domain,
            "supported": self.supported,
            "actions": list(self.actions),
            "description": self.description,
            "reason": self.reason,
        }


def resolve_action_profile(domain: str) -> ActionProfile:
    normalized = (domain or "").strip().lower()
    if normalized == "light":
        return ActionProfile(
            target_kind="light",
            domain=normalized,
            supported=True,
            actions=("turn_on", "turn_off", "toggle", "brightness"),
            description="Light-style target",
        )
    if normalized == "switch":
        return ActionProfile(
            target_kind="switch",
            domain=normalized,
            supported=True,
            actions=("turn_on", "turn_off", "toggle"),
            description="Switch-style target",
        )
    if normalized == "cover":
        return ActionProfile(
            target_kind="cover",
            domain=normalized,
            supported=True,
            actions=("open", "close", "position"),
            description="Cover / blind target",
        )
    if normalized == "climate":
        return ActionProfile(
            target_kind="climate",
            domain=normalized,
            supported=True,
            actions=("temperature_up", "temperature_down", "temperature_set"),
            description="Climate / radiator / boiler target",
        )
    if normalized == "media_player":
        return ActionProfile(
            target_kind="media_player",
            domain=normalized,
            supported=True,
            actions=("play_pause", "volume_up", "volume_down"),
            description="Media player / TV / speaker target",
        )
    if normalized == "fan":
        return ActionProfile(
            target_kind="fan",
            domain=normalized,
            supported=True,
            actions=("turn_on", "turn_off", "toggle", "increase", "decrease"),
            description="Fan target",
        )
    if normalized == "lock":
        return ActionProfile(
            target_kind="lock",
            domain=normalized,
            supported=True,
            actions=("lock", "unlock", "toggle"),
            description="Lock target",
        )
    if normalized == "vacuum":
        return ActionProfile(
            target_kind="vacuum",
            domain=normalized,
            supported=True,
            actions=("start", "pause", "stop", "return_to_base", "toggle"),
            description="Vacuum target",
        )
    if normalized == "humidifier":
        return ActionProfile(
            target_kind="humidifier",
            domain=normalized,
            supported=True,
            actions=("turn_on", "turn_off", "toggle", "humidity_up", "humidity_down"),
            description="Humidifier target",
        )
    if normalized in {"sensor", "binary_sensor", "button", "update", "event"}:
        return ActionProfile(
            target_kind=normalized,
            domain=normalized,
            supported=False,
            actions=(),
            description="Read-only or trigger-only target",
            reason="This target is read-only or trigger-only and does not expose control actions.",
        )
    if not normalized:
        return ActionProfile(
            target_kind="unknown",
            domain=normalized,
            supported=False,
            actions=(),
            description="Unknown target",
            reason="Missing target domain.",
        )
    return ActionProfile(
        target_kind=normalized,
        domain=normalized,
        supported=False,
        actions=(),
        description="Unsupported target",
        reason=f"Target domain '{normalized}' is not supported in phase 1.",
    )


def is_supported_actionable_domain(domain: str) -> bool:
    return (domain or "").strip().lower() in SUPPORTED_ACTIONABLE_TARGET_DOMAINS
