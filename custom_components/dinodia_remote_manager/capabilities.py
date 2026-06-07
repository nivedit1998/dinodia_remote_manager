"""Capability discovery helpers for Dinodia Remote Manager."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.components.device_automation import async_get_device_automations
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .binding_rules import ActionProfile, resolve_action_profile, is_supported_actionable_domain
from .const import REMOTE_LABEL_NAME

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ResolvedCapability:
    target_kind: str
    domain: str
    supported: bool
    actions: tuple[str, ...]
    description: str
    reason: str | None
    target_device_id: str | None = None
    target_entity_id: str | None = None
    source: str = "unknown"

    def as_dict(self) -> dict[str, object]:
        return {
            "target_kind": self.target_kind,
            "domain": self.domain,
            "supported": self.supported,
            "actions": list(self.actions),
            "description": self.description,
            "reason": self.reason,
            "target_device_id": self.target_device_id,
            "target_entity_id": self.target_entity_id,
            "source": self.source,
        }

    def as_api_dict(self) -> dict[str, object]:
        return {
            "targetKind": self.target_kind,
            "domain": self.domain,
            "supported": self.supported,
            "actions": list(self.actions),
            "description": self.description,
            "reason": self.reason,
            "targetDeviceId": self.target_device_id,
            "targetEntityId": self.target_entity_id,
            "source": self.source,
        }


def _device_has_remote_label(device: dr.DeviceEntry) -> bool:
    # The HA "Remote" label is an installer convenience and visual label.
    # Dinodia classifies trigger-device behavior from bindings, HA device triggers,
    # and capabilities, not from label alone.
    labels = getattr(device, "labels", None) or []
    for label in labels:
        if isinstance(label, str):
            normalized = label.strip().lower()
            if normalized == REMOTE_LABEL_NAME.lower() or "remote" in normalized:
                return True
        elif hasattr(label, "name") and str(getattr(label, "name", "")).strip().lower() == REMOTE_LABEL_NAME.lower():
            return True
    return False


def _friendly_device_name(device: dr.DeviceEntry) -> str:
    name = (device.name_by_user or device.name or device.id or "").strip()
    return name or device.id


def _friendly_entity_name(entity: er.RegistryEntry, state_name: str | None) -> str:
    name = (state_name or entity.original_name or entity.name or entity.entity_id).strip()
    return name or entity.entity_id


def _supported_domain_from_entity(entity_id: str) -> str:
    return (entity_id.split(".")[0] if "." in entity_id else entity_id).strip().lower()


async def async_get_device_triggers(hass: HomeAssistant, device_id: str) -> list[dict[str, object]]:
    try:
        triggers = await async_get_device_automations(hass, "trigger", device_id)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Unable to list device triggers for %s: %s", device_id, err)
        return []
    return [dict(trigger) for trigger in triggers or []]


async def async_device_has_triggers(hass: HomeAssistant, device_id: str) -> bool:
    return bool(await async_get_device_triggers(hass, device_id))


def _integration_domains_for_device(hass: HomeAssistant, device: dr.DeviceEntry) -> set[str]:
    domains: set[str] = set()
    for entry_id in getattr(device, "config_entries", ()) or ():
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain:
            domains.add(entry.domain.lower())
    for identifier in getattr(device, "identifiers", ()) or ():
        try:
            domain = str(next(iter(identifier))).strip().lower()
        except Exception:  # noqa: BLE001
            domain = ""
        if domain:
            domains.add(domain)
    return domains


def _registry_looks_remote_like(hass: HomeAssistant, device: dr.DeviceEntry) -> bool:
    domains = _integration_domains_for_device(hass, device)
    text = " ".join(
        str(value or "")
        for value in (
            getattr(device, "manufacturer", ""),
            getattr(device, "model", ""),
            getattr(device, "name", ""),
            getattr(device, "name_by_user", ""),
            " ".join(domains),
            " ".join(
                " ".join(map(str, identifier))
                for identifier in (getattr(device, "identifiers", ()) or ())
            ),
        )
    ).lower()
    remote_words = (
        "remote",
        "dimmer",
        "button",
        "shortcut",
        "scene",
        "dial",
        "knob",
        "rodret",
        "styrbar",
        "tradfri",
        "symfonisk",
        "hue tap",
        "hue dimmer",
    )
    integration_words = ("zha", "zigbee", "matter", "thread", "homekit_controller")
    return any(word in text for word in remote_words) and (
        bool(domains.intersection(integration_words)) or any(word in text for word in integration_words)
    )


def _entity_is_diagnostic_or_trigger_only(entity_id: str) -> bool:
    return _supported_domain_from_entity(entity_id) in {"button", "sensor", "binary_sensor", "event"}


def _profile_for_entity(entity_id: str) -> ActionProfile:
    return resolve_action_profile(_supported_domain_from_entity(entity_id))


async def async_get_remote_device_choices(hass: HomeAssistant) -> dict[str, str]:
    device_reg = dr.async_get(hass)
    explicit_choices: dict[str, str] = {}
    fallback_choices: dict[str, str] = {}
    for device in device_reg.devices.values():
        fallback_choices[device.id] = _friendly_device_name(device)
        if _device_has_remote_label(device):
            explicit_choices[device.id] = _friendly_device_name(device)
    choices = explicit_choices or fallback_choices
    return dict(sorted(choices.items(), key=lambda item: item[1].lower()))


async def async_get_supported_target_device_choices(hass: HomeAssistant) -> dict[str, str]:
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    states = hass.states
    device_to_entities: dict[str, list[str]] = {}

    for entity in entity_reg.entities.values():
        if not entity.device_id:
            continue
        device_to_entities.setdefault(entity.device_id, []).append(entity.entity_id)

    choices: dict[str, str] = {}
    for device in device_reg.devices.values():
        entity_ids = device_to_entities.get(device.id, [])
        if not entity_ids:
            continue
        if _resolve_entity_choice_for_device(hass, entity_ids) is None:
            continue
        choices[device.id] = _friendly_device_name(device)

    return dict(sorted(choices.items(), key=lambda item: item[1].lower()))


async def async_get_supported_target_entity_choices(hass: HomeAssistant) -> dict[str, str]:
    entity_reg = er.async_get(hass)
    choices: dict[str, str] = {}
    for entity in entity_reg.entities.values():
        profile = _profile_for_entity(entity.entity_id)
        if not profile.supported:
            continue
        state = hass.states.get(entity.entity_id)
        state_name = state.name if state is not None else None
        choices[entity.entity_id] = _friendly_entity_name(entity, state_name)
    return dict(sorted(choices.items(), key=lambda item: item[1].lower()))


async def async_get_trigger_device_inventory(hass: HomeAssistant) -> list[dict[str, object]]:
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    device_to_entities: dict[str, list[str]] = {}

    for entity in entity_reg.entities.values():
        if not entity.device_id:
            continue
        device_to_entities.setdefault(entity.device_id, []).append(entity.entity_id)

    result: list[dict[str, object]] = []
    for device in device_reg.devices.values():
        device_id = device.id
        entity_ids = device_to_entities.get(device_id, [])
        target_entity = _resolve_entity_choice_for_device(hass, entity_ids)
        has_actionable_target = target_entity is not None and is_supported_actionable_domain(
            _supported_domain_from_entity(target_entity)
        )
        triggers = await async_get_device_triggers(hass, device_id)
        remote_label = _device_has_remote_label(device)
        registry_remote_like = _registry_looks_remote_like(hass, device)
        diagnostic_only = bool(entity_ids) and all(
            _entity_is_diagnostic_or_trigger_only(entity_id) for entity_id in entity_ids
        )

        if has_actionable_target:
            continue
        if not (triggers or registry_remote_like or (remote_label and diagnostic_only)):
            continue

        domains = sorted(_integration_domains_for_device(hass, device))
        reason = (
            "device_triggers"
            if triggers
            else "registry_remote_like"
            if registry_remote_like
            else "remote_label_diagnostic_only"
        )
        result.append(
            {
                "device_id": device_id,
                "name": _friendly_device_name(device),
                "trigger_count": len(triggers),
                "triggers": triggers,
                "entity_ids": entity_ids,
                "has_actionable_target": has_actionable_target,
                "remote_label": remote_label,
                "registry_remote_like": registry_remote_like,
                "integration_domains": domains,
                "manufacturer": getattr(device, "manufacturer", None),
                "model": getattr(device, "model", None),
                "reason": reason,
            }
        )
    return result


def _resolve_entity_choice_for_device(hass: HomeAssistant, entity_ids: list[str]) -> str | None:
    entity_reg = er.async_get(hass)
    supported_domains = (
        "light",
        "switch",
        "cover",
        "climate",
        "media_player",
    )
    best_entity_id: str | None = None
    best_rank = 999
    for entity_id in entity_ids:
        domain = _supported_domain_from_entity(entity_id)
        if domain not in supported_domains and domain not in {"sensor", "binary_sensor", "button"}:
            continue
        profile = resolve_action_profile(domain)
        if not profile.supported:
            continue
        rank = supported_domains.index(domain) if domain in supported_domains else 500
        if rank < best_rank:
            best_rank = rank
            best_entity_id = entity_id
        elif rank == best_rank and best_entity_id is not None:
            current = entity_reg.entities.get(entity_id)
            best = entity_reg.entities.get(best_entity_id)
            current_name = (current.name or current.original_name or current.entity_id) if current else entity_id
            best_name = (best.name or best.original_name or best.entity_id) if best else best_entity_id
            if current_name.lower() < best_name.lower():
                best_entity_id = entity_id
    return best_entity_id


async def async_resolve_target_capability(
    hass: HomeAssistant,
    *,
    target_device_id: str | None = None,
    target_entity_id: str | None = None,
) -> ResolvedCapability:
    if target_entity_id:
        entity_reg = er.async_get(hass)
        entity = entity_reg.async_get(target_entity_id)
        resolved_device_id = entity.device_id if entity is not None else None
        profile = _profile_for_entity(target_entity_id)
        return ResolvedCapability(
            target_kind=profile.target_kind,
            domain=profile.domain,
            supported=profile.supported,
            actions=profile.actions,
            description=profile.description,
            reason=profile.reason,
            target_device_id=resolved_device_id,
            target_entity_id=target_entity_id,
            source="entity",
        )

    if target_device_id:
        entity_reg = er.async_get(hass)
        entity_ids = [
            entity.entity_id
            for entity in entity_reg.entities.values()
            if entity.device_id == target_device_id
        ]
        candidate = _resolve_entity_choice_for_device(hass, entity_ids)
        if candidate is None:
            return ResolvedCapability(
                target_kind="unknown",
                domain="",
                supported=False,
                actions=(),
                description="Unsupported target",
                reason="No supported actionable entity was found on this device.",
                target_device_id=target_device_id,
                source="device",
            )
        resolved = await async_resolve_target_capability(
            hass, target_entity_id=candidate
        )
        return ResolvedCapability(
            target_kind=resolved.target_kind,
            domain=resolved.domain,
            supported=resolved.supported,
            actions=resolved.actions,
            description=resolved.description,
            reason=resolved.reason,
            target_device_id=target_device_id,
            target_entity_id=resolved.target_entity_id,
            source="device",
        )

    return ResolvedCapability(
        target_kind="unknown",
        domain="",
        supported=False,
        actions=(),
        description="Unsupported target",
        reason="No target device or entity was supplied.",
    )
