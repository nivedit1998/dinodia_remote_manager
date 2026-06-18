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


def _binding_matches_remote(binding: "RemoteBinding", remote_device_id: str) -> bool:
    return _normalized_identifier(binding.remote_device_id) == _normalized_identifier(remote_device_id)


class RemoteBindingStorage(Store[dict[str, Any]]):
    """HA storage wrapper with explicit migration support."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Migrate old binding storage payloads.

        Version 2 only adds optional metadata fields on each binding. Those
        fields are already defaulted by RemoteBinding.from_dict(), so the data
        shape can be preserved.
        """
        if not isinstance(old_data, dict):
            return {}
        return old_data


@dataclass(slots=True)
class RemoteBinding:
    binding_id: str
    remote_device_id: str
    target_device_id: str | None
    target_entity_id: str | None
    target_kind: str
    binding_name: str | None = None
    enabled: bool = True
    owner_user_id: str | None = None
    source: str = "legacy"
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
            "ownerUserId": self.owner_user_id,
            "source": self.source,
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
            owner_user_id=str(payload.get("owner_user_id") or "").strip() or None,
            source=str(payload.get("source") or "legacy").strip() or "legacy",
            created_at=str(payload.get("created_at") or "").strip(),
            updated_at=str(payload.get("updated_at") or "").strip(),
        )


class RemoteBindingStore:
    """Store of remote bindings."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = RemoteBindingStorage(hass, DATA_STORE_VERSION, DATA_STORE_KEY)
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

    def async_find_bindings_for_remote_or_binding(
        self,
        *,
        binding_id: str | None = None,
        remote_device_id: str | None = None,
    ) -> list[RemoteBinding]:
        binding_ids: set[str] = set()
        normalized_remote = _normalized_identifier(remote_device_id)
        normalized_binding = _normalized_identifier(binding_id)
        for key, binding in self._bindings.items():
            if binding_id and (key == binding_id or _normalized_identifier(key) == normalized_binding):
                binding_ids.add(key)
                continue
            if remote_device_id and _binding_matches_remote(binding, normalized_remote):
                binding_ids.add(key)
                continue
            if binding_id and _normalized_identifier(binding.binding_id) == normalized_binding:
                binding_ids.add(key)
        return [self._bindings[key] for key in sorted(binding_ids)]

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
            owner_user_id=binding.owner_user_id,
            source=binding.source,
            created_at=created_at,
            updated_at=now,
        )
        self._bindings[updated.binding_id] = updated
        await self.async_save()
        return updated

    async def async_restore_bindings(self, bindings: list[RemoteBinding]) -> None:
        restored_ids = {binding.binding_id for binding in bindings}
        for binding in bindings:
            self._bindings[binding.binding_id] = binding
        for key in list(self._bindings.keys()):
            if key.startswith("remote:"):
                candidate = self._bindings[key]
                if any(_binding_matches_remote(candidate, restored.remote_device_id) for restored in bindings):
                    if key not in restored_ids:
                        self._bindings.pop(key, None)
        await self.async_save()

    async def async_replace_binding_for_remote(
        self,
        *,
        remote_device_id: str,
        binding_id: str | None = None,
        target_device_id: str | None = None,
        target_entity_id: str | None = None,
        target_kind: str,
        binding_name: str | None = None,
        enabled: bool = True,
        owner_user_id: str | None = None,
        source: str = "legacy",
    ) -> RemoteBinding:
        existing = self.async_find_binding(
            binding_id=binding_id,
            remote_device_id=remote_device_id,
        )
        stable_binding_id = (
            existing.binding_id
            if existing is not None
            else (binding_id or f"remote:{remote_device_id}")
        )

        if binding_name:
            stable_binding_name = binding_name
        elif existing is not None:
            stable_binding_name = existing.binding_name
        else:
            stable_binding_name = None

        for key, candidate in list(self._bindings.items()):
            if key == stable_binding_id:
                continue
            if _binding_matches_remote(candidate, remote_device_id):
                self._bindings.pop(key, None)

        binding = RemoteBinding(
            binding_id=stable_binding_id,
            remote_device_id=remote_device_id,
            target_device_id=target_device_id,
            target_entity_id=target_entity_id,
            target_kind=target_kind,
            binding_name=stable_binding_name,
            enabled=existing.enabled if existing is not None else enabled,
            owner_user_id=owner_user_id if owner_user_id is not None else (existing.owner_user_id if existing is not None else None),
            source=source or (existing.source if existing is not None else "legacy"),
        )
        return await self.async_upsert_binding(binding)

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

    async def async_remove_bindings_for_owner(self, owner_user_id: str) -> list[RemoteBinding]:
        owner = str(owner_user_id or "").strip()
        removed: list[RemoteBinding] = []
        if not owner:
            return removed
        for key, binding in list(self._bindings.items()):
            if str(binding.owner_user_id or "").strip() == owner:
                removed.append(binding)
                self._bindings.pop(key, None)
        if removed:
            await self.async_save()
        return removed

    async def async_remove_bindings_for_owner_devices(
        self,
        owner_user_id: str,
        remote_device_ids: list[str],
    ) -> list[RemoteBinding]:
        owner = str(owner_user_id or "").strip()
        device_ids = {_normalized_identifier(device_id) for device_id in remote_device_ids if _normalized_identifier(device_id)}
        removed: list[RemoteBinding] = []
        if not owner or not device_ids:
            return removed
        for key, binding in list(self._bindings.items()):
            if str(binding.owner_user_id or "").strip() != owner:
                continue
            if _normalized_identifier(binding.remote_device_id) not in device_ids:
                continue
            removed.append(binding)
            self._bindings.pop(key, None)
        if removed:
            await self.async_save()
        return removed
