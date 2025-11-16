"""Environment registry persistence helpers."""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - circular import guard
    from server import ProjectRegistry


VALID_LIFECYCLE_STATES = {"planned", "active", "maintenance", "retired"}
VALID_HEALTH_STATUSES = {"unknown", "healthy", "degraded", "offline", "maintenance"}
DEFAULT_LIFECYCLE_STATE = "planned"
DEFAULT_HEALTH_STATUS = "unknown"
HEALTH_STALENESS_SECONDS = 3600


@dataclass
class EnvironmentRecord:
    environment_id: str
    project_id: str
    slug: str
    name: str
    description: str
    host: Dict[str, Any]
    ports: List[Dict[str, Any]]
    owner: Dict[str, Any]
    lifecycle: Dict[str, Any]
    health: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: utcnow_iso())
    updated_at: str = field(default_factory=lambda: utcnow_iso())


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def seconds_since(timestamp: Optional[str]) -> Optional[float]:
    reference = parse_iso_timestamp(timestamp)
    if not reference:
        return None
    delta = (datetime.now(timezone.utc) - reference).total_seconds()
    return delta if delta >= 0 else 0.0


class EnvironmentStore:
    """Thread-safe JSON-backed store for environment metadata."""

    def __init__(self, db_path: Path, project_registry: Optional["ProjectRegistry"] = None) -> None:
        self.db_path = db_path
        self.project_registry = project_registry
        self._lock = threading.Lock()
        self._records: Dict[str, EnvironmentRecord] = {}
        self._slug_index: Dict[str, str] = {}
        self._revision = 0
        self._load()

    def _load(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists():
            self.db_path.write_text("{}\n", encoding="utf-8")
        try:
            data = json.loads(self.db_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        with self._lock:
            self._records.clear()
            self._slug_index.clear()
            for env_id, payload in data.items():
                if not isinstance(payload, dict):
                    continue
                try:
                    slug = self._normalize_slug(payload.get("slug") or env_id)
                    record = EnvironmentRecord(
                        environment_id=str(env_id),
                        project_id=self._normalize_project_id(payload.get("project_id")),
                        slug=slug,
                        name=self._require_text(payload.get("name"), "name"),
                        description=str(payload.get("description") or "").strip(),
                        host=self._normalize_host(payload.get("host") or {}),
                        ports=self._normalize_ports(payload.get("ports") or []),
                        owner=self._normalize_owner(payload.get("owner") or {}),
                        lifecycle=self._normalize_lifecycle(payload.get("lifecycle") or {}),
                        health=self._normalize_health(payload.get("health") or {}),
                        metadata=self._normalize_metadata(payload.get("metadata")),
                        created_at=payload.get("created_at") or utcnow_iso(),
                        updated_at=payload.get("updated_at") or utcnow_iso(),
                    )
                except ValueError:
                    continue
                if slug in self._slug_index and self._slug_index[slug] != record.environment_id:
                    continue
                self._records[record.environment_id] = record
                self._slug_index[record.slug] = record.environment_id
            self._revision += 1

    def _persist(self) -> None:
        with self._lock:
            serialized = {env_id: asdict(record) for env_id, record in self._records.items()}
        self.db_path.write_text(json.dumps(serialized, indent=2) + "\n", encoding="utf-8")

    def _require_text(self, value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field} is required")
        return text

    def _normalize_project_id(self, project_id: Any) -> str:
        value = self._require_text(project_id, "project_id")
        if self.project_registry:
            project = self.project_registry.get(value)
            if not project:
                raise ValueError(f"unknown project_id: {value}")
        return value

    def _normalize_slug(self, slug: Any) -> str:
        text = self._require_text(slug, "slug").lower()
        text = re.sub(r"[^a-z0-9-]+", "-", text)
        text = re.sub(r"-+", "-", text).strip("-")
        if not text:
            raise ValueError("slug is required")
        return text

    def _normalize_host(self, host: Dict[str, Any]) -> Dict[str, Any]:
        hostname = self._require_text(host.get("hostname"), "host.hostname")
        return {
            "hostname": hostname,
            "provider": str(host.get("provider") or "").strip(),
            "region": str(host.get("region") or "").strip(),
            "ip": str(host.get("ip") or "").strip(),
            "notes": str(host.get("notes") or "").strip(),
        }

    def _normalize_ports(self, ports: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for entry in ports:
            if not isinstance(entry, dict):
                continue
            name = self._require_text(entry.get("name"), "port.name")
            port_value = entry.get("port")
            try:
                port_int = int(port_value)
            except (TypeError, ValueError):
                raise ValueError(f"invalid port for {name}") from None
            if port_int <= 0 or port_int > 65535:
                raise ValueError(f"invalid port for {name}")
            protocol = str(entry.get("protocol") or "tcp").strip().lower()
            normalized.append(
                {
                    "name": name,
                    "port": port_int,
                    "protocol": protocol or "tcp",
                    "url": str(entry.get("url") or "").strip(),
                    "description": str(entry.get("description") or "").strip(),
                }
            )
        return normalized

    def _normalize_owner(self, owner: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": self._require_text(owner.get("name"), "owner.name"),
            "email": str(owner.get("email") or "").strip(),
            "slack": str(owner.get("slack") or "").strip(),
            "role": str(owner.get("role") or "").strip(),
        }

    def _normalize_lifecycle(self, lifecycle: Dict[str, Any]) -> Dict[str, Any]:
        state = str(lifecycle.get("state") or DEFAULT_LIFECYCLE_STATE).strip().lower()
        if state not in VALID_LIFECYCLE_STATES:
            raise ValueError(f"invalid lifecycle state: {state}")
        changed_at = lifecycle.get("changed_at") or utcnow_iso()
        return {
            "state": state,
            "changed_at": changed_at,
            "notes": str(lifecycle.get("notes") or "").strip(),
        }

    def _normalize_health(self, health: Dict[str, Any]) -> Dict[str, Any]:
        status = str(health.get("status") or DEFAULT_HEALTH_STATUS).strip().lower()
        if status not in VALID_HEALTH_STATUSES:
            raise ValueError(f"invalid health status: {status}")
        checked_at = health.get("checked_at") or health.get("last_checked_at")
        if checked_at:
            checked_at = str(checked_at).strip()
        return {
            "status": status,
            "checked_at": checked_at,
            "url": str(health.get("url") or "").strip(),
            "notes": str(health.get("notes") or "").strip(),
        }

    def _normalize_metadata(self, metadata: Any) -> Dict[str, Any]:
        if not isinstance(metadata, dict):
            return {}
        cleaned: Dict[str, Any] = {}
        for key, value in metadata.items():
            cleaned[str(key)] = value
        return cleaned

    def _generate_id(self, slug: str) -> str:
        suffix = uuid.uuid4().hex[:6]
        return f"env-{slug}-{suffix}"

    def get_revision(self) -> int:
        with self._lock:
            return self._revision

    def list_environments(self, project_id: Optional[str] = None) -> List[EnvironmentRecord]:
        with self._lock:
            records = list(self._records.values())
        if project_id:
            project_id = str(project_id).strip()
            records = [record for record in records if record.project_id == project_id]
        return sorted(records, key=lambda record: (record.project_id, record.slug))

    def get_environment(self, env_id: str) -> Optional[EnvironmentRecord]:
        with self._lock:
            return self._records.get(env_id)

    def get_by_slug(self, slug: str) -> Optional[EnvironmentRecord]:
        normalized = slug.strip().lower()
        with self._lock:
            env_id = self._slug_index.get(normalized)
            if not env_id:
                return None
            return self._records.get(env_id)

    def create_environment(self, payload: Dict[str, Any]) -> EnvironmentRecord:
        slug = self._normalize_slug(payload.get("slug") or payload.get("name"))
        project_id = self._normalize_project_id(payload.get("project_id"))
        name = self._require_text(payload.get("name"), "name")
        description = str(payload.get("description") or "").strip()
        host = self._normalize_host(payload.get("host") or {})
        ports = self._normalize_ports(payload.get("ports") or [])
        owner = self._normalize_owner(payload.get("owner") or {})
        lifecycle = self._normalize_lifecycle(payload.get("lifecycle") or {})
        health = self._normalize_health(payload.get("health") or {})
        metadata = self._normalize_metadata(payload.get("metadata"))
        now = utcnow_iso()
        env_id = payload.get("environment_id") or self._generate_id(slug)
        record = EnvironmentRecord(
            environment_id=str(env_id),
            project_id=project_id,
            slug=slug,
            name=name,
            description=description,
            host=host,
            ports=ports,
            owner=owner,
            lifecycle=lifecycle,
            health=health,
            metadata=metadata,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            if slug in self._slug_index:
                raise ValueError(f"slug already in use: {slug}")
            self._records[record.environment_id] = record
            self._slug_index[slug] = record.environment_id
            self._revision += 1
        self._persist()
        return record

    def update_environment(self, env_id: str, updates: Dict[str, Any]) -> EnvironmentRecord:
        prepared_slug = None
        if "slug" in updates and updates["slug"] is not None:
            prepared_slug = self._normalize_slug(updates["slug"])
        prepared_project = None
        if "project_id" in updates and updates["project_id"] is not None:
            prepared_project = self._normalize_project_id(updates["project_id"])
        prepared_name = None
        if "name" in updates and updates["name"] is not None:
            prepared_name = self._require_text(updates["name"], "name")
        prepared_description = None
        if "description" in updates and updates["description"] is not None:
            prepared_description = str(updates["description"]).strip()
        prepared_host = None
        if "host" in updates and updates["host"] is not None:
            prepared_host = self._normalize_host(updates["host"])
        prepared_ports = None
        if "ports" in updates and updates["ports"] is not None:
            prepared_ports = self._normalize_ports(updates["ports"])
        prepared_owner = None
        if "owner" in updates and updates["owner"] is not None:
            prepared_owner = self._normalize_owner(updates["owner"])
        prepared_lifecycle = None
        if "lifecycle" in updates and updates["lifecycle"] is not None:
            prepared_lifecycle = self._normalize_lifecycle(updates["lifecycle"])
        prepared_health = None
        if "health" in updates and updates["health"] is not None:
            prepared_health = self._normalize_health(updates["health"])
        prepared_metadata = None
        if "metadata" in updates and updates["metadata"] is not None:
            prepared_metadata = self._normalize_metadata(updates["metadata"])

        with self._lock:
            record = self._records.get(env_id)
            if not record:
                raise KeyError(env_id)
            mutated = False
            if prepared_slug is not None:
                existing = self._slug_index.get(prepared_slug)
                if existing and existing != env_id:
                    raise ValueError(f"slug already in use: {prepared_slug}")
                if record.slug != prepared_slug:
                    self._slug_index.pop(record.slug, None)
                    self._slug_index[prepared_slug] = env_id
                    record.slug = prepared_slug
                    mutated = True
            if prepared_project is not None and record.project_id != prepared_project:
                record.project_id = prepared_project
                mutated = True
            if prepared_name is not None and record.name != prepared_name:
                record.name = prepared_name
                mutated = True
            if prepared_description is not None and record.description != prepared_description:
                record.description = prepared_description
                mutated = True
            if prepared_host is not None and record.host != prepared_host:
                record.host = prepared_host
                mutated = True
            if prepared_ports is not None and record.ports != prepared_ports:
                record.ports = prepared_ports
                mutated = True
            if prepared_owner is not None and record.owner != prepared_owner:
                record.owner = prepared_owner
                mutated = True
            if prepared_lifecycle is not None and record.lifecycle != prepared_lifecycle:
                record.lifecycle = prepared_lifecycle
                mutated = True
            if prepared_health is not None and record.health != prepared_health:
                record.health = prepared_health
                mutated = True
            if prepared_metadata is not None and record.metadata != prepared_metadata:
                record.metadata = prepared_metadata
                mutated = True
            if not mutated:
                return record
            record.updated_at = utcnow_iso()
        self._persist()
        return record

    def delete_environment(self, env_id: str) -> bool:
        with self._lock:
            record = self._records.pop(env_id, None)
            if not record:
                return False
            self._slug_index.pop(record.slug, None)
            self._revision += 1
        self._persist()
        return True

    def health_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            records = list(self._records.values())
            revision = self._revision
        status_counts = Counter(record.health.get("status", DEFAULT_HEALTH_STATUS) for record in records)
        lifecycle_counts = Counter(record.lifecycle.get("state", DEFAULT_LIFECYCLE_STATE) for record in records)
        stale_checks = 0
        for record in records:
            age = seconds_since(record.health.get("checked_at"))
            if age is not None and age > HEALTH_STALENESS_SECONDS:
                stale_checks += 1
        return {
            "total": len(records),
            "status_counts": dict(status_counts),
            "lifecycle_counts": dict(lifecycle_counts),
            "stale_checks": stale_checks,
            "revision": revision,
        }

    def to_collection_payload(
        self,
        registry: Optional["ProjectRegistry"],
        *,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        records = self.list_environments(project_id=project_id)
        payload = [build_environment_payload(record, registry) for record in records]
        return {
            "environments": payload,
            "total": len(payload),
            "project_id": project_id,
            "revision": self.get_revision(),
        }


def build_environment_payload(record: EnvironmentRecord, registry: Optional["ProjectRegistry"] = None) -> Dict[str, Any]:
    payload = asdict(record)
    if registry:
        project = registry.get(record.project_id)
        if project:
            payload["project"] = project.to_payload()
    payload["health"] = {
        **record.health,
        "age_seconds": seconds_since(record.health.get("checked_at")),
    }
    return payload
