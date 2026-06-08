"""Capability discovery helpers for Dinodia Remote Manager."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any

from homeassistant.components.device_automation import async_get_device_automations
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, label_registry as lr

from .binding_rules import ActionProfile, resolve_action_profile
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

REAL_DASHBOARD_ACTION_DOMAINS = {
    "light",
    "switch",
    "climate",
    "cover",
    "media_player",
    "fan",
    "lock",
    "humidifier",
    "vacuum",
}

PASSIVE_HELPER_DOMAINS = {
    "sensor",
    "binary_sensor",
    "event",
}

IGNORED_BUTTON_ACTION_WORDS = {
    "identify",
    "identify_button",
    "ping",
    "locate",
    "find",
    "find_my",
    "diagnostic",
}

PASSIVE_HELPER_DEVICE_CLASSES = {
    "battery",
    "signal_strength",
    "voltage",
    "current",
    "power_factor",
    "linkquality",
    "rssi",
    "lqi",
    "last_seen",
    "timestamp",
    "enum",
    "connectivity",
    "problem",
    "update",
}

PASSIVE_HELPER_WORDS = {
    "battery",
    "linkquality",
    "lqi",
    "rssi",
    "last_seen",
    "last_seen_time",
    "voltage",
    "signal_strength",
}


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


def _label_name(hass: HomeAssistant, label_id: str) -> str:
    label_reg = lr.async_get(hass)
    label = label_reg.async_get_label(label_id)
    return str(label.name if label else label_id).strip()


def _device_and_entity_labels(hass: HomeAssistant, device_id: str) -> list[str]:
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    labels: set[str] = set()

    device = device_reg.async_get(device_id)
    if device is not None:
        for label_id in getattr(device, "labels", set()) or set():
            name = _label_name(hass, str(label_id))
            if name:
                labels.add(name)

    for entity in er.async_entries_for_device(entity_reg, device_id, include_disabled_entities=True):
        for label_id in getattr(entity, "labels", set()) or set():
            name = _label_name(hass, str(label_id))
            if name:
                labels.add(name)

    return sorted(labels, key=str.lower)


def _friendly_device_name(device: dr.DeviceEntry) -> str:
    name = (device.name_by_user or device.name or device.id or "").strip()
    return name or device.id


def _friendly_entity_name(entity: er.RegistryEntry, state_name: str | None) -> str:
    name = (state_name or entity.original_name or entity.name or entity.entity_id).strip()
    return name or entity.entity_id


def _supported_domain_from_entity(entity_id: str) -> str:
    return (entity_id.split(".")[0] if "." in entity_id else entity_id).strip().lower()


def _entity_registry_entry(hass: HomeAssistant, entity_id: str):
    entity_reg = er.async_get(hass)
    return entity_reg.async_get(entity_id)


def _stable_entity_classification_text(hass: HomeAssistant, entity_id: str) -> str:
    parts: list[str] = [entity_id]

    entity_entry = _entity_registry_entry(hass, entity_id)
    if entity_entry is not None:
        for value in (
            getattr(entity_entry, "original_device_class", None),
            getattr(entity_entry, "entity_category", None),
            getattr(entity_entry, "platform", None),
        ):
            if value:
                parts.append(str(value))

    state = hass.states.get(entity_id)
    if state is not None:
        for key in ("device_class", "entity_category"):
            value = state.attributes.get(key)
            if value:
                parts.append(str(value))

    return " ".join(parts).replace("_", " ").replace("-", " ").lower()


def _normalize_token(value: object | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _classification_text_has_any(text: str, words: set[str]) -> bool:
    normalized = _normalize_token(text)
    for word in words:
        token = _normalize_token(word)
        if not token:
            continue
        if (
            normalized == token
            or normalized.startswith(f"{token}_")
            or normalized.endswith(f"_{token}")
            or f"_{token}_" in normalized
        ):
            return True
    return False


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


def _is_ignored_dashboard_helper_entity(hass: HomeAssistant, entity_id: str) -> bool:
    domain = _supported_domain_from_entity(entity_id)
    entity_entry = _entity_registry_entry(hass, entity_id)
    entity_category = _normalize_token(getattr(entity_entry, "entity_category", None))
    device_class = _normalize_token(getattr(entity_entry, "original_device_class", None))
    state = hass.states.get(entity_id)
    if state is not None:
        entity_category = entity_category or _normalize_token(state.attributes.get("entity_category"))
        device_class = device_class or _normalize_token(state.attributes.get("device_class"))
    text = _stable_entity_classification_text(hass, entity_id)

    if entity_category == "diagnostic":
        return True

    if domain in PASSIVE_HELPER_DOMAINS:
        return device_class in PASSIVE_HELPER_DEVICE_CLASSES or _classification_text_has_any(
            text, PASSIVE_HELPER_WORDS
        )

    if domain != "button":
        return False

    if device_class == "identify":
        return True
    return _classification_text_has_any(text, IGNORED_BUTTON_ACTION_WORDS)


def _is_blocking_button_action_entity(hass: HomeAssistant, entity_id: str) -> bool:
    domain = _supported_domain_from_entity(entity_id)
    if domain != "button":
        return False
    if _is_ignored_dashboard_helper_entity(hass, entity_id):
        return False
    # Conservative by design: any non-helper button may cause a real side effect.
    return True


def _entity_has_real_dashboard_action(hass: HomeAssistant, entity_id: str) -> bool:
    if _is_ignored_dashboard_helper_entity(hass, entity_id):
        return False
    domain = _supported_domain_from_entity(entity_id)
    if domain not in REAL_DASHBOARD_ACTION_DOMAINS:
        return False
    profile = resolve_action_profile(domain)
    return profile.supported and bool(profile.actions)


def _profile_for_entity(entity_id: str) -> ActionProfile:
    return resolve_action_profile(_supported_domain_from_entity(entity_id))


async def async_get_remote_device_choices(hass: HomeAssistant) -> dict[str, str]:
    choices: dict[str, str] = {}
    for item in await async_get_trigger_device_inventory(hass):
        device_id = str(item.get("device_id") or "").strip()
        name = str(item.get("name") or device_id).strip()
        if device_id:
            choices[device_id] = name or device_id
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
        if _is_ignored_dashboard_helper_entity(hass, entity.entity_id):
            continue
        if _is_blocking_button_action_entity(hass, entity.entity_id):
            continue
        profile = _profile_for_entity(entity.entity_id)
        if not profile.supported:
            continue
        state = hass.states.get(entity.entity_id)
        state_name = state.name if state is not None else None
        choices[entity.entity_id] = _friendly_entity_name(entity, state_name)
    return dict(sorted(choices.items(), key=lambda item: item[1].lower()))


def _bound_remote_device_ids(hass: HomeAssistant) -> set[str]:
    store = hass.data.get(DOMAIN, {}).get("store")
    if store is None or not hasattr(store, "async_list_bindings"):
        return set()
    try:
        return {
            str(binding.remote_device_id).strip()
            for binding in store.async_list_bindings()
            if str(getattr(binding, "remote_device_id", "") or "").strip()
        }
    except Exception:  # noqa: BLE001
        return set()


def _trigger_diagnostic_to_inventory_item(row: dict[str, Any]) -> dict[str, object]:
    return {
        "device_id": row["device_id"],
        "name": row["name"],
        "labels": row["labels"],
        "has_labels": bool(row["labels"]),
        "trigger_count": row["trigger_count"],
        "triggers": row.get("triggers", []),
        "entity_ids": row["entity_ids"],
        "has_actionable_target": bool(row["real_action_entity_ids"] or row["blocking_button_entity_ids"]),
        "registry_remote_like": row["registry_remote_like"],
        "diagnostic_only": bool(row["entity_ids"])
        and not bool(row["real_action_entity_ids"] or row["blocking_button_entity_ids"]),
        "trigger_required": True,
        "real_action_entity_ids": row["real_action_entity_ids"],
        "blocking_button_entity_ids": row["blocking_button_entity_ids"],
        "ignored_helper_entity_ids": row["ignored_helper_entity_ids"],
        "trigger_classification": "labelled_triggers_no_actions",
        "integration_domains": row["integration_domains"],
        "manufacturer": row["manufacturer"],
        "model": row["model"],
        "reason": "device_triggers",
    }


async def async_get_trigger_device_diagnostics(hass: HomeAssistant) -> list[dict[str, object]]:
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    device_to_entities: dict[str, list[str]] = {}

    for entity in entity_reg.entities.values():
        if not entity.device_id:
            continue
        device_to_entities.setdefault(entity.device_id, []).append(entity.entity_id)

    bound_device_ids = _bound_remote_device_ids(hass)
    result: list[dict[str, object]] = []
    for device in device_reg.devices.values():
        device_id = device.id
        entity_ids = device_to_entities.get(device_id, [])
        labels = _device_and_entity_labels(hass, device_id)

        if not labels and device_id not in bound_device_ids:
            continue

        ignored_helper_entity_ids = [
            entity_id
            for entity_id in entity_ids
            if _is_ignored_dashboard_helper_entity(hass, entity_id)
        ]
        blocking_button_entity_ids = [
            entity_id
            for entity_id in entity_ids
            if _is_blocking_button_action_entity(hass, entity_id)
        ]
        real_action_entity_ids = [
            entity_id
            for entity_id in entity_ids
            if _entity_has_real_dashboard_action(hass, entity_id)
        ]
        triggers = await async_get_device_triggers(hass, device_id)
        registry_remote_like = _registry_looks_remote_like(hass, device)

        if not labels:
            reject_reason = "unlabelled"
        elif not triggers:
            reject_reason = "no_triggers"
        elif blocking_button_entity_ids:
            reject_reason = "blocking_button_entities"
        elif real_action_entity_ids:
            reject_reason = "real_action_entities"
        else:
            reject_reason = "accepted"

        domains = sorted(_integration_domains_for_device(hass, device))
        result.append(
            {
                "device_id": device_id,
                "name": _friendly_device_name(device),
                "labels": labels,
                "has_labels": bool(labels),
                "trigger_count": len(triggers),
                "triggers": triggers,
                "entity_ids": entity_ids,
                "real_action_entity_ids": real_action_entity_ids,
                "blocking_button_entity_ids": blocking_button_entity_ids,
                "ignored_helper_entity_ids": ignored_helper_entity_ids,
                "reject_reason": reject_reason,
                "has_actionable_target": bool(real_action_entity_ids or blocking_button_entity_ids),
                "registry_remote_like": registry_remote_like,
                "diagnostic_only": bool(entity_ids)
                and not bool(real_action_entity_ids or blocking_button_entity_ids),
                "trigger_required": True,
                "trigger_classification": (
                    "labelled_triggers_no_actions"
                    if reject_reason == "accepted"
                    else "rejected"
                ),
                "integration_domains": domains,
                "manufacturer": getattr(device, "manufacturer", None),
                "model": getattr(device, "model", None),
                "reason": reject_reason,
            }
        )
    return result


async def async_get_trigger_device_inventory(hass: HomeAssistant) -> list[dict[str, object]]:
    return [
        _trigger_diagnostic_to_inventory_item(row)
        for row in await async_get_trigger_device_diagnostics(hass)
        if row.get("reject_reason") == "accepted"
    ]


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
        if _is_ignored_dashboard_helper_entity(hass, entity_id):
            continue
        if _is_blocking_button_action_entity(hass, entity_id):
            continue
        if domain not in supported_domains:
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
        if _is_ignored_dashboard_helper_entity(hass, target_entity_id):
            return ResolvedCapability(
                target_kind="unsupported",
                domain=_supported_domain_from_entity(target_entity_id),
                supported=False,
                actions=(),
                description="Diagnostic/helper entities cannot be remote targets",
                reason="diagnostic_helper_entity",
                target_device_id=resolved_device_id,
                target_entity_id=target_entity_id,
                source="entity",
            )
        if _is_blocking_button_action_entity(hass, target_entity_id):
            return ResolvedCapability(
                target_kind="unsupported",
                domain="button",
                supported=False,
                actions=(),
                description="Button action entities cannot be remote targets",
                reason="button_action_entity",
                target_device_id=resolved_device_id,
                target_entity_id=target_entity_id,
                source="entity",
            )
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
