"""Persistent binding store for Dinodia Remote Manager."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DATA_STORE_KEY, DATA_STORE_VERSION


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _normalized_identifier(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    for prefix in ("device:", "remote:", "id:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :].strip()
    return normalized


@dataclass(slots=True)
class RemoteBinding:
    binding_id: str
    remote_device_id: str
    target_device_id: str | None
    target_entity_id: str | None
    target_kind: str
    binding_name: str | None = None
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_api_dict(self) -> dict[str, Any]:
        return {
            "bindingId": self.binding_id,
            "remoteDeviceId": self.remote_device_id,
            "targetDeviceId": self.target_device_id,
            "targetEntityId": self.target_entity_id,
            "targetKind": self.target_kind,
            "bindingName": self.binding_name,
            "enabled": self.enabled,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RemoteBinding":
        return cls(
            binding_id=str(payload.get("binding_id") or "").strip(),
            remote_device_id=str(payload.get("remote_device_id") or "").strip(),
            target_device_id=str(payload.get("target_device_id") or "").strip() or None,
            target_entity_id=str(payload.get("target_entity_id") or "").strip() or None,
            target_kind=str(payload.get("target_kind") or "unknown").strip() or "unknown",
            binding_name=str(payload.get("binding_name") or "").strip() or None,
            enabled=bool(payload.get("enabled", True)),
            created_at=str(payload.get("created_at") or "").strip(),
            updated_at=str(payload.get("updated_at") or "").strip(),
        )


class RemoteBindingStore:
    """Store of remote bindings."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store[dict[str, Any]](hass, DATA_STORE_VERSION, DATA_STORE_KEY)
        self._bindings: dict[str, RemoteBinding] = {}
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return
        payload = await self._store.async_load() or {}
        bindings = payload.get("bindings") if isinstance(payload, dict) else []
        self._bindings = {}
        for item in bindings or []:
            if not isinstance(item, dict):
                continue
            binding = RemoteBinding.from_dict(item)
            if not binding.binding_id or not binding.remote_device_id:
                continue
            self._bindings[binding.binding_id] = binding
        self._loaded = True

    async def async_save(self) -> None:
        payload = {
            "version": DATA_STORE_VERSION,
            "bindings": [binding.as_dict() for binding in self._bindings.values()],
        }
        await self._store.async_save(payload)

    def async_get_binding(self, binding_id: str) -> RemoteBinding | None:
        return self._bindings.get(binding_id)

    def async_get_binding_by_remote(self, remote_device_id: str, *aliases: str) -> RemoteBinding | None:
        candidates = {
            _normalized_identifier(remote_device_id),
            remote_device_id.strip(),
        }
        for alias in aliases:
            normalized_alias = _normalized_identifier(alias)
            if normalized_alias:
                candidates.add(normalized_alias)
            raw_alias = str(alias or "").strip()
            if raw_alias:
                candidates.add(raw_alias)
        candidates = {candidate for candidate in candidates if candidate}
        if not candidates:
            return None
        for binding in self._bindings.values():
            binding_candidates = {
                binding.remote_device_id,
                _normalized_identifier(binding.remote_device_id),
                binding.binding_id,
                _normalized_identifier(binding.binding_id),
            }
            if any(candidate in binding_candidates for candidate in candidates):
                return binding
        return None

    def async_find_binding(
        self,
        *,
        binding_id: str | None = None,
        remote_device_id: str | None = None,
        remote_device_aliases: list[str] | tuple[str, ...] | None = None,
    ) -> RemoteBinding | None:
        if binding_id:
            binding = self.async_get_binding(binding_id)
            if binding is not None:
                return binding
        aliases = list(remote_device_aliases or ())
        if remote_device_id:
            aliases.append(remote_device_id)
        if aliases:
            primary = aliases[0]
            return self.async_get_binding_by_remote(primary, *aliases[1:])
        return None

    def async_list_bindings(self) -> list[RemoteBinding]:
        return sorted(self._bindings.values(), key=lambda binding: binding.binding_id)

    async def async_upsert_binding(self, binding: RemoteBinding) -> RemoteBinding:
        now = _utcnow_iso()
        existing = self._bindings.get(binding.binding_id)
        if existing is not None and existing.created_at:
            created_at = existing.created_at
        else:
            created_at = now
        updated = RemoteBinding(
            binding_id=binding.binding_id,
            remote_device_id=binding.remote_device_id,
            target_device_id=binding.target_device_id,
            target_entity_id=binding.target_entity_id,
            target_kind=binding.target_kind,
            binding_name=binding.binding_name,
            enabled=binding.enabled,
            created_at=created_at,
            updated_at=now,
        )
        self._bindings[updated.binding_id] = updated
        await self.async_save()
        return updated

    async def async_remove_binding(
        self,
        binding_id: str | None = None,
        remote_device_id: str | None = None,
    ) -> int:
        removed = 0
        if binding_id:
            if binding_id in self._bindings:
                self._bindings.pop(binding_id, None)
                removed += 1
        elif remote_device_id:
            to_remove = [
                key
                for key, binding in self._bindings.items()
                if binding.remote_device_id == remote_device_id
            ]
            for key in to_remove:
                self._bindings.pop(key, None)
                removed += 1
        if removed:
            await self.async_save()
        return removed
