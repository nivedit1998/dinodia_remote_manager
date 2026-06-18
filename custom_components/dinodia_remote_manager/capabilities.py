"""Capability discovery helpers for Dinodia Remote Manager."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
import re
import time
from typing import Any

try:
    from homeassistant.components.device_automation import (
        DeviceAutomationType,
        async_get_device_automations,
    )
except ImportError:  # Older HA versions may not expose DeviceAutomationType.
    from homeassistant.components.device_automation import async_get_device_automations

    DeviceAutomationType = None  # type: ignore[assignment]
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, label_registry as lr

from .binding_rules import ActionProfile, resolve_action_profile
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

TRIGGER_DISCOVERY_CACHE_TTL_SECONDS = 30
TRIGGER_DISCOVERY_CACHE_MAX_ITEMS = 256
TRIGGER_DISCOVERY_CACHE_KEY = "trigger_discovery_cache"
TRIGGER_DASHBOARD_CACHE_TTL_SECONDS = 10
TRIGGER_DASHBOARD_CACHE_MAX_ITEMS = 128
TRIGGER_DASHBOARD_CACHE_KEY = "trigger_dashboard_cache"

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


@dataclass(slots=True, frozen=True)
class TriggerDiscoveryResult:
    triggers: tuple[dict[str, object], ...]
    sources: tuple[str, ...]
    ha_python_trigger_count: int
    ha_ws_equivalent_trigger_count: int
    zha_quirk_trigger_count: int
    integration_trigger_count: int
    zha_quirk_class: str | None = None
    zha_ieee: str | None = None
    integration_domains: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    fetched_at: float = 0.0


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


def _device_area_metadata(hass: HomeAssistant, device: dr.DeviceEntry) -> tuple[str | None, str | None]:
    area_id = str(getattr(device, "area_id", "") or "").strip() or None
    if not area_id:
        return None, None
    area = ar.async_get(hass).async_get_area(area_id)
    area_name = str(area.name).strip() if area is not None and area.name else None
    return area_id, area_name


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


def _json_safe_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    return str(value)


def _json_safe_trigger(trigger: dict[str, object]) -> dict[str, object]:
    return {str(key): _json_safe_value(value) for key, value in trigger.items()}


def _dedupe_triggers(triggers: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for raw_trigger in triggers:
        trigger = _json_safe_trigger(raw_trigger)
        key = "|".join(
            str(trigger.get(field) or "")
            for field in (
                "platform",
                "domain",
                "device_id",
                "type",
                "subtype",
                "command",
                "cluster_id",
                "endpoint_id",
                "source",
            )
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(trigger)
    return result


def _trigger_discovery_cache(hass: HomeAssistant) -> OrderedDict[str, TriggerDiscoveryResult]:
    data = hass.data.setdefault(DOMAIN, {})
    cache = data.get(TRIGGER_DISCOVERY_CACHE_KEY)
    if not isinstance(cache, OrderedDict):
        cache = OrderedDict()
        data[TRIGGER_DISCOVERY_CACHE_KEY] = cache
    return cache


def _get_cached_trigger_discovery(
    hass: HomeAssistant,
    device_id: str,
) -> TriggerDiscoveryResult | None:
    cache = _trigger_discovery_cache(hass)
    cached = cache.get(device_id)
    if cached is None:
        return None
    if time.monotonic() - cached.fetched_at > TRIGGER_DISCOVERY_CACHE_TTL_SECONDS:
        cache.pop(device_id, None)
        return None
    cache.move_to_end(device_id)
    return cached


def _set_cached_trigger_discovery(
    hass: HomeAssistant,
    device_id: str,
    result: TriggerDiscoveryResult,
) -> None:
    cache = _trigger_discovery_cache(hass)
    cache[device_id] = result
    cache.move_to_end(device_id)
    while len(cache) > TRIGGER_DISCOVERY_CACHE_MAX_ITEMS:
        cache.popitem(last=False)


def clear_trigger_discovery_cache(hass: HomeAssistant, device_id: str | None = None) -> None:
    """Clear trigger discovery cache for one device or all devices."""
    cache = _trigger_discovery_cache(hass)
    normalized_device_id = str(device_id or "").strip()
    if normalized_device_id:
        cache.pop(normalized_device_id, None)
        return
    cache.clear()


def _trigger_dashboard_cache(hass: HomeAssistant) -> OrderedDict[str, tuple[float, list[dict[str, object]]]]:
    data = hass.data.setdefault(DOMAIN, {})
    cache = data.get(TRIGGER_DASHBOARD_CACHE_KEY)
    if not isinstance(cache, OrderedDict):
        cache = OrderedDict()
        data[TRIGGER_DASHBOARD_CACHE_KEY] = cache
    return cache


def _get_cached_trigger_dashboard_inventory(
    hass: HomeAssistant,
    cache_key: str,
) -> list[dict[str, object]] | None:
    cache = _trigger_dashboard_cache(hass)
    cached = cache.get(cache_key)
    if cached is None:
        return None
    fetched_at, items = cached
    if time.monotonic() - fetched_at > TRIGGER_DASHBOARD_CACHE_TTL_SECONDS:
        cache.pop(cache_key, None)
        return None
    cache.move_to_end(cache_key)
    return [dict(item) for item in items]


def _set_cached_trigger_dashboard_inventory(
    hass: HomeAssistant,
    cache_key: str,
    items: list[dict[str, object]],
) -> None:
    cache = _trigger_dashboard_cache(hass)
    cache[cache_key] = (time.monotonic(), [dict(item) for item in items])
    cache.move_to_end(cache_key)
    while len(cache) > TRIGGER_DASHBOARD_CACHE_MAX_ITEMS:
        cache.popitem(last=False)


def clear_trigger_dashboard_cache(hass: HomeAssistant, device_id: str | None = None) -> None:
    cache = _trigger_dashboard_cache(hass)
    normalized_device_id = str(device_id or "").strip()
    if not normalized_device_id:
        cache.clear()
        return
    keys_to_remove = [key for key in cache.keys() if key == "all" or key == normalized_device_id]
    for key in keys_to_remove:
        cache.pop(key, None)


def _ha_trigger_automation_type() -> object:
    if DeviceAutomationType is not None:
        return DeviceAutomationType.TRIGGER
    return "trigger"


async def _async_get_ha_python_device_triggers(
    hass: HomeAssistant,
    device_id: str,
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    normalized_device_id = str(device_id or "").strip()
    if not normalized_device_id:
        return [], ("ha_python_trigger_error:empty_device_id",)

    try:
        automations_by_device = await async_get_device_automations(
            hass,
            _ha_trigger_automation_type(),
            {normalized_device_id},
        )
    except Exception as err:  # noqa: BLE001
        message = str(err).strip() or repr(err)
        _LOGGER.debug("Unable to list HA Python device triggers for %s: %s", normalized_device_id, message)
        return [], (f"ha_python_trigger_error:{type(err).__name__}:{message}",)

    raw_triggers: list[object] = []
    if isinstance(automations_by_device, dict):
        raw_triggers = list(automations_by_device.get(normalized_device_id) or [])
        if not raw_triggers and len(automations_by_device) == 1:
            raw_triggers = list(next(iter(automations_by_device.values())) or [])
    elif isinstance(automations_by_device, list):
        raw_triggers = automations_by_device

    normalized: list[dict[str, object]] = []
    for trigger in raw_triggers:
        if not isinstance(trigger, dict):
            continue
        item = dict(trigger)
        item.setdefault("source", "ha_device_automation_python")
        normalized.append(_json_safe_trigger(item))
    return normalized, ()


async def _async_get_ha_ws_equivalent_device_triggers(
    hass: HomeAssistant,
    device_id: str,
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    del hass, device_id
    return [], ("ha_ws_equivalent_unavailable",)


async def async_get_device_triggers(hass: HomeAssistant, device_id: str) -> list[dict[str, object]]:
    result = await async_get_device_trigger_discovery(hass, device_id)
    return list(result.triggers)


async def async_get_device_trigger_discovery(
    hass: HomeAssistant,
    device_id: str,
    *,
    use_cache: bool = True,
) -> TriggerDiscoveryResult:
    normalized_device_id = str(device_id or "").strip()
    now = time.monotonic()
    if not normalized_device_id:
        return TriggerDiscoveryResult(
            triggers=(),
            sources=(),
            ha_python_trigger_count=0,
            ha_ws_equivalent_trigger_count=0,
            zha_quirk_trigger_count=0,
            integration_trigger_count=0,
            errors=("empty_device_id",),
            fetched_at=now,
        )

    if use_cache:
        cached = _get_cached_trigger_discovery(hass, normalized_device_id)
        if cached is not None:
            return cached

    device_reg = dr.async_get(hass)
    device = device_reg.async_get(normalized_device_id)
    if device is None:
        result = TriggerDiscoveryResult(
            triggers=(),
            sources=(),
            ha_python_trigger_count=0,
            ha_ws_equivalent_trigger_count=0,
            zha_quirk_trigger_count=0,
            integration_trigger_count=0,
            errors=("device_not_found",),
            fetched_at=now,
        )
        _set_cached_trigger_discovery(hass, normalized_device_id, result)
        return result

    domains = tuple(sorted(_integration_domains_for_device(hass, device)))
    ha_python_triggers, ha_python_errors = await _async_get_ha_python_device_triggers(
        hass,
        normalized_device_id,
    )
    ha_ws_triggers: list[dict[str, object]] = []
    ha_ws_errors: tuple[str, ...] = ()
    if not ha_python_triggers:
        ha_ws_triggers, ha_ws_errors = await _async_get_ha_ws_equivalent_device_triggers(
            hass,
            normalized_device_id,
        )

    integration_triggers: list[dict[str, object]] = []
    integration_metadata: dict[str, object] = {
        "sources": [],
        "errors": [],
        "zha_quirk_trigger_count": 0,
        "zha_quirk_class": None,
        "zha_ieee": None,
    }
    if not ha_python_triggers and not ha_ws_triggers:
        integration_triggers, integration_metadata = _get_integration_trigger_fallbacks(
            hass,
            normalized_device_id,
            device,
            domains,
        )

    triggers = _dedupe_triggers([*ha_python_triggers, *ha_ws_triggers, *integration_triggers])
    sources: list[str] = []
    if ha_python_triggers:
        sources.append("ha_device_automation_python")
    if ha_ws_triggers:
        sources.append("ha_device_automation_websocket_equivalent")
    for source in integration_metadata.get("sources", []):
        source_name = str(source)
        if source_name not in sources:
            sources.append(source_name)

    result = TriggerDiscoveryResult(
        triggers=tuple(triggers),
        sources=tuple(sources),
        ha_python_trigger_count=len(ha_python_triggers),
        ha_ws_equivalent_trigger_count=len(ha_ws_triggers),
        zha_quirk_trigger_count=int(integration_metadata.get("zha_quirk_trigger_count") or 0),
        integration_trigger_count=len(integration_triggers),
        zha_quirk_class=(
            str(integration_metadata.get("zha_quirk_class"))
            if integration_metadata.get("zha_quirk_class") is not None
            else None
        ),
        zha_ieee=(
            str(integration_metadata.get("zha_ieee"))
            if integration_metadata.get("zha_ieee") is not None
            else None
        ),
        integration_domains=domains,
        errors=tuple(
            [
                *ha_python_errors,
                *ha_ws_errors,
                *[str(err) for err in integration_metadata.get("errors", [])],
            ]
        ),
        fetched_at=now,
    )
    _set_cached_trigger_discovery(hass, normalized_device_id, result)
    return result


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


def _zha_ieee_from_device_entry(device: dr.DeviceEntry) -> str | None:
    for identifier in getattr(device, "identifiers", ()) or ():
        values = [str(value).strip() for value in identifier if str(value).strip()]
        if len(values) >= 2 and values[0].lower() == "zha":
            return values[1]
    return None


def _zha_data_candidates(hass: HomeAssistant) -> list[object]:
    zha_data = hass.data.get("zha")
    if zha_data is None:
        return []

    candidates: list[object] = [zha_data]
    if isinstance(zha_data, dict):
        candidates.extend(value for value in zha_data.values() if value is not None)
        for key in ("zha_gateway", "gateway", "device_manager", "application_controller"):
            value = zha_data.get(key)
            if value is not None:
                candidates.append(value)

    for item in list(candidates):
        for attr in ("zha_gateway", "gateway", "device_manager", "application_controller"):
            value = getattr(item, attr, None)
            if value is not None:
                candidates.append(value)
    return candidates


def _lookup_zha_device_in_candidate(candidate: object, lookup_keys: set[str]) -> object | None:
    if isinstance(candidate, dict):
        for key, value in candidate.items():
            if str(key) in lookup_keys:
                return value
            ieee = getattr(value, "ieee", None) or getattr(value, "ieee_address", None)
            if ieee is not None and str(ieee) in lookup_keys:
                return value

    for attr in ("devices", "device_proxies", "_devices", "_device_proxies"):
        mapping = getattr(candidate, attr, None)
        if not isinstance(mapping, dict):
            continue
        for key, value in mapping.items():
            if str(key) in lookup_keys:
                return value
            ieee = getattr(value, "ieee", None) or getattr(value, "ieee_address", None)
            if ieee is not None and str(ieee) in lookup_keys:
                return value
    return None


def _resolve_zha_device_for_ha_device(
    hass: HomeAssistant,
    device: dr.DeviceEntry,
) -> tuple[object | None, str | None, tuple[str, ...]]:
    ieee = _zha_ieee_from_device_entry(device)
    lookup_keys = {device.id}
    if ieee:
        lookup_keys.add(str(ieee))

    errors: list[str] = []
    for candidate in _zha_data_candidates(hass):
        try:
            found = _lookup_zha_device_in_candidate(candidate, lookup_keys)
        except Exception as err:  # noqa: BLE001
            message = str(err).strip() or repr(err)
            errors.append(f"zha_lookup_error:{type(err).__name__}:{message}")
            continue
        if found is not None:
            return found, ieee, tuple(errors)
    return None, ieee, tuple([*errors, "zha_device_not_found"])


def _unwrap_zha_or_zigpy_device(value: object) -> object:
    current = value
    for attr in ("zigpy_device", "_zigpy_device", "device", "_device"):
        next_value = getattr(current, attr, None)
        if next_value is not None and next_value is not current:
            current = next_value
    return current


def _zha_quirk_trigger_to_dict(
    device_id: str,
    trigger_key: object,
    payload: object,
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None

    trigger_type = "zha_event"
    trigger_subtype = ""
    if isinstance(trigger_key, tuple):
        if len(trigger_key) > 0:
            trigger_type = str(trigger_key[0])
        if len(trigger_key) > 1:
            trigger_subtype = str(trigger_key[1])
    elif trigger_key is not None:
        trigger_type = str(trigger_key)

    trigger: dict[str, object] = {
        "platform": "device",
        "domain": "zha",
        "device_id": device_id,
        "type": trigger_type,
        "subtype": trigger_subtype,
        "source": "zha_quirk_device_automation_triggers",
    }
    for key, value in payload.items():
        trigger[str(key)] = _json_safe_value(value)
    return _json_safe_trigger(trigger)


def _get_zha_quirk_device_triggers(
    hass: HomeAssistant,
    device_id: str,
    device: dr.DeviceEntry,
) -> dict[str, object]:
    zha_device, ieee, lookup_errors = _resolve_zha_device_for_ha_device(hass, device)
    if zha_device is None:
        return {
            "triggers": [],
            "zha_quirk_class": None,
            "zha_ieee": ieee,
            "errors": lookup_errors,
        }

    zigpy_device = _unwrap_zha_or_zigpy_device(zha_device)
    class_name = f"{type(zigpy_device).__module__}.{type(zigpy_device).__name__}"

    trigger_map = getattr(zigpy_device, "device_automation_triggers", None)
    if trigger_map is None:
        trigger_map = getattr(type(zigpy_device), "device_automation_triggers", None)

    if not isinstance(trigger_map, dict) or not trigger_map:
        return {
            "triggers": [],
            "zha_quirk_class": class_name,
            "zha_ieee": ieee,
            "errors": (*lookup_errors, "zha_quirk_triggers_missing"),
        }

    triggers: list[dict[str, object]] = []
    for trigger_key, payload in trigger_map.items():
        trigger = _zha_quirk_trigger_to_dict(device_id, trigger_key, payload)
        if trigger is not None:
            triggers.append(trigger)

    return {
        "triggers": triggers,
        "zha_quirk_class": class_name,
        "zha_ieee": ieee,
        "errors": lookup_errors,
    }


def _get_integration_trigger_fallbacks(
    hass: HomeAssistant,
    device_id: str,
    device: dr.DeviceEntry,
    domains: tuple[str, ...],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    triggers: list[dict[str, object]] = []
    sources: list[str] = []
    errors: list[str] = []
    metadata: dict[str, object] = {
        "sources": sources,
        "errors": errors,
        "zha_quirk_trigger_count": 0,
        "zha_quirk_class": None,
        "zha_ieee": None,
    }

    if "zha" in domains:
        zha_result = _get_zha_quirk_device_triggers(hass, device_id, device)
        zha_triggers = list(zha_result.get("triggers", []))
        triggers.extend(zha_triggers)
        metadata["zha_quirk_trigger_count"] = len(zha_triggers)
        metadata["zha_quirk_class"] = zha_result.get("zha_quirk_class")
        metadata["zha_ieee"] = zha_result.get("zha_ieee")
        errors.extend(str(err) for err in zha_result.get("errors", []))
        if zha_triggers:
            sources.append("zha_quirk_device_automation_triggers")

    return triggers, metadata


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


def _entity_area_metadata(
    hass: HomeAssistant,
    entity: er.RegistryEntry | None,
    state_name: str | None = None,
) -> tuple[str | None, str | None]:
    del state_name
    area_id = str(getattr(entity, "area_id", "") or "").strip() or None
    if not area_id:
        return None, None
    area = ar.async_get(hass).async_get_area(area_id)
    area_name = str(area.name).strip() if area is not None and area.name else None
    return area_id, area_name


def _labels_for_target(hass: HomeAssistant, *, device_id: str | None, entity_id: str | None) -> list[str]:
    labels: list[str] = []
    if device_id:
        labels.extend(_device_and_entity_labels(hass, device_id))
    if entity_id:
        entity = _entity_registry_entry(hass, entity_id)
        if entity is not None:
            for label_id in getattr(entity, "labels", set()) or set():
                name = _label_name(hass, str(label_id))
                if name:
                    labels.append(name)
    deduped: list[str] = []
    seen: set[str] = set()
    for label in labels:
        key = label.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(label)
    return deduped


def _source_entity_id_for_trigger_candidate(hass: HomeAssistant, entity_ids: list[str]) -> str | None:
    non_helper_entities = [entity_id for entity_id in entity_ids if not _is_ignored_dashboard_helper_entity(hass, entity_id)]
    if non_helper_entities:
        return non_helper_entities[0]
    return entity_ids[0] if entity_ids else None


def _target_summary_for_binding(
    hass: HomeAssistant,
    *,
    target_device_id: str | None,
    target_entity_id: str | None,
) -> dict[str, object] | None:
    if not target_device_id and not target_entity_id:
        return None

    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    state = hass.states.get(target_entity_id or "") if target_entity_id else None
    entity_entry = entity_reg.async_get(target_entity_id) if target_entity_id else None
    device = device_reg.async_get(target_device_id) if target_device_id else (
        device_reg.async_get(entity_entry.device_id) if entity_entry and entity_entry.device_id else None
    )

    resolved_device_id = str(
        target_device_id
        or (entity_entry.device_id if entity_entry is not None else "")
        or ""
    ).strip() or None
    resolved_entity_id = str(target_entity_id or "").strip() or None
    area_id, area_name = _device_area_metadata(hass, device) if device is not None else (None, None)
    if area_name is None and entity_entry is not None:
        _, area_name = _entity_area_metadata(hass, entity_entry)

    display_name = (
        _friendly_device_name(device) if device is not None else None
    ) or (
        _friendly_entity_name(entity_entry, state.name if state is not None else None)
        if entity_entry is not None
        else None
    ) or resolved_device_id or resolved_entity_id or "Target unavailable"

    return {
        "targetId": resolved_device_id or resolved_entity_id or "unresolved",
        "deviceId": resolved_device_id,
        "entityId": resolved_entity_id,
        "name": display_name,
        "domain": _supported_domain_from_entity(resolved_entity_id or ""),
        "areaName": area_name,
        "labels": _labels_for_target(hass, device_id=resolved_device_id, entity_id=resolved_entity_id),
    }


def _trigger_diagnostic_to_inventory_item(row: dict[str, Any]) -> dict[str, object]:
    return {
        "device_id": row["device_id"],
        "name": row["name"],
        "area_id": row.get("area_id"),
        "area_name": row.get("area_name"),
        "labels": row["labels"],
        "has_labels": bool(row["labels"]),
        "trigger_count": row["trigger_count"],
        "triggers": row.get("triggers", []),
        "trigger_sources": row.get("trigger_sources", []),
        "ha_python_trigger_count": row.get("ha_python_trigger_count", 0),
        "ha_ws_equivalent_trigger_count": row.get("ha_ws_equivalent_trigger_count", 0),
        "zha_quirk_trigger_count": row.get("zha_quirk_trigger_count", 0),
        "integration_trigger_count": row.get("integration_trigger_count", 0),
        "zha_quirk_class": row.get("zha_quirk_class"),
        "zha_ieee": row.get("zha_ieee"),
        "trigger_discovery_errors": row.get("trigger_discovery_errors", []),
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
        trigger_discovery = await async_get_device_trigger_discovery(hass, device_id)
        triggers = list(trigger_discovery.triggers)
        registry_remote_like = _registry_looks_remote_like(hass, device)
        area_id, area_name = _device_area_metadata(hass, device)

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
                "area_id": area_id,
                "area_name": area_name,
                "labels": labels,
                "has_labels": bool(labels),
                "trigger_count": len(triggers),
                "triggers": triggers,
                "trigger_sources": list(trigger_discovery.sources),
                "ha_python_trigger_count": trigger_discovery.ha_python_trigger_count,
                "ha_ws_equivalent_trigger_count": trigger_discovery.ha_ws_equivalent_trigger_count,
                "zha_quirk_trigger_count": trigger_discovery.zha_quirk_trigger_count,
                "integration_trigger_count": trigger_discovery.integration_trigger_count,
                "zha_quirk_class": trigger_discovery.zha_quirk_class,
                "zha_ieee": trigger_discovery.zha_ieee,
                "trigger_discovery_errors": list(trigger_discovery.errors),
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


async def async_get_trigger_device_dashboard_inventory(
    hass: HomeAssistant,
    *,
    device_id: str | None = None,
    force: bool = False,
) -> list[dict[str, object]]:
    normalized_device_id = str(device_id or "").strip()
    cache_key = normalized_device_id or "all"
    if not force:
        cached = _get_cached_trigger_dashboard_inventory(hass, cache_key)
        if cached is not None:
            return cached

    inventory = await async_get_trigger_device_inventory(hass)
    store = hass.data.get(DOMAIN, {}).get("store")
    bindings_by_remote: dict[str, object] = {}
    if store is not None and hasattr(store, "async_list_bindings"):
        try:
            for binding in store.async_list_bindings():
                remote_id = str(getattr(binding, "remote_device_id", "") or "").strip()
                if remote_id and remote_id not in bindings_by_remote:
                    bindings_by_remote[remote_id] = binding
        except Exception:  # noqa: BLE001
            bindings_by_remote = {}

    rows: list[dict[str, object]] = []
    for item in inventory:
        current_device_id = str(item.get("device_id") or "").strip()
        if normalized_device_id and current_device_id != normalized_device_id:
            continue

        binding = bindings_by_remote.get(current_device_id)
        binding_api = binding.as_api_dict() if binding is not None and hasattr(binding, "as_api_dict") else None
        capability = None
        resolution_state = "unbound"
        target_summary = None
        if binding is not None:
            capability_obj = await async_resolve_target_capability(
                hass,
                target_device_id=getattr(binding, "target_device_id", None),
                target_entity_id=getattr(binding, "target_entity_id", None),
            )
            capability = capability_obj.as_api_dict()
            resolution_state = "bound" if capability_obj.supported else "target_unresolved"
            target_summary = _target_summary_for_binding(
                hass,
                target_device_id=capability_obj.target_device_id or getattr(binding, "target_device_id", None),
                target_entity_id=capability_obj.target_entity_id or getattr(binding, "target_entity_id", None),
            )
            if target_summary is None:
                resolution_state = "target_unresolved"

        source_entity_id = _source_entity_id_for_trigger_candidate(
            hass,
            [str(entity_id) for entity_id in item.get("entity_ids", []) if str(entity_id).strip()],
        )

        area_id = str(item.get("area_id") or "").strip() or None
        area_name = str(item.get("area_name") or "").strip() or None
        if not area_name and source_entity_id:
            source_entity = _entity_registry_entry(hass, source_entity_id)
            source_area_id, source_area_name = _entity_area_metadata(hass, source_entity)
            area_id = area_id or source_area_id
            area_name = area_name or source_area_name

        rows.append(
            {
                "device_id": current_device_id,
                "accepted": True,
                "name": str(item.get("name") or current_device_id).strip() or current_device_id,
                "area_id": area_id,
                "area_name": area_name,
                "labels": list(item.get("labels") or []),
                "source_entity_id": source_entity_id,
                "trigger_count": int(item.get("trigger_count") or 0),
                "entity_ids": [str(entity_id) for entity_id in item.get("entity_ids", []) if str(entity_id).strip()],
                "binding": binding_api,
                "capability": capability,
                "target": target_summary,
                "resolution_state": (
                    "target_unavailable"
                    if binding is not None and target_summary is None
                    else resolution_state
                ),
            }
        )

    _set_cached_trigger_dashboard_inventory(hass, cache_key, rows)
    if not normalized_device_id:
        for row in rows:
            row_device_id = str(row.get("device_id") or "").strip()
            if row_device_id:
                _set_cached_trigger_dashboard_inventory(hass, row_device_id, [row])
    return [dict(row) for row in rows]


def _resolve_entity_choice_for_device(hass: HomeAssistant, entity_ids: list[str]) -> str | None:
    entity_reg = er.async_get(hass)
    supported_domains = (
        "light",
        "switch",
        "cover",
        "climate",
        "media_player",
        "fan",
        "lock",
        "vacuum",
        "humidifier",
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
