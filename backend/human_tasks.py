"""Persistence helpers for the Human Task queue."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid
from typing import Any, Dict, Iterable, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from server import ProjectRegistry


HUMAN_TASK_STATUS_OPEN = "open"
HUMAN_TASK_STATUS_IN_PROGRESS = "in_progress"
HUMAN_TASK_STATUS_RESOLVED = "resolved"
VALID_HUMAN_TASK_STATUSES = {
    HUMAN_TASK_STATUS_OPEN,
    HUMAN_TASK_STATUS_IN_PROGRESS,
    HUMAN_TASK_STATUS_RESOLVED,
}
STATUS_SORT_ORDER = {
    HUMAN_TASK_STATUS_OPEN: 0,
    HUMAN_TASK_STATUS_IN_PROGRESS: 1,
    HUMAN_TASK_STATUS_RESOLVED: 2,
}


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


@dataclass
class HumanTaskRecord:
    task_id: str
    title: str
    description: str
    status: str
    blocking: bool
    created_at: str
    updated_at: str
    project_id: Optional[str] = None
    prompt_id: Optional[str] = None
    resolved_at: Optional[str] = None


class HumanTaskStore:
    """Thread-safe JSON-backed store for Human Task metadata."""

    def __init__(self, db_path: Path, project_registry: Optional["ProjectRegistry"] = None) -> None:
        self.db_path = db_path
        self.project_registry = project_registry
        self._lock = threading.Lock()
        self._records: Dict[str, HumanTaskRecord] = {}
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
            for task_id, payload in data.items():
                if not isinstance(payload, dict):
                    continue
                record = HumanTaskRecord(
                    task_id=task_id,
                    title=str(payload.get("title") or "").strip(),
                    description=str(payload.get("description") or "").strip(),
                    status=self._normalize_status(payload.get("status")),
                    blocking=bool(payload.get("blocking")),
                    project_id=self._normalize_project_id(payload.get("project_id")),
                    prompt_id=self._normalize_prompt_id(payload.get("prompt_id")),
                    created_at=payload.get("created_at") or utcnow_iso(),
                    updated_at=payload.get("updated_at") or utcnow_iso(),
                    resolved_at=payload.get("resolved_at"),
                )
                if record.status == HUMAN_TASK_STATUS_RESOLVED and not record.resolved_at:
                    record.resolved_at = record.updated_at
                self._records[task_id] = record
            self._revision += 1

    def _persist(self) -> None:
        with self._lock:
            serialized = {task_id: asdict(record) for task_id, record in self._records.items()}
        self.db_path.write_text(json.dumps(serialized, indent=2) + "\n", encoding="utf-8")

    def _normalize_status(self, status: Optional[str]) -> str:
        if not status:
            return HUMAN_TASK_STATUS_OPEN
        normalized = str(status).strip().lower()
        if normalized not in VALID_HUMAN_TASK_STATUSES:
            return HUMAN_TASK_STATUS_OPEN
        return normalized

    def _normalize_project_id(self, project_id: Optional[str]) -> Optional[str]:
        if not project_id:
            return None
        if not self.project_registry:
            return str(project_id).strip()
        return self.project_registry.resolve_project_id(str(project_id).strip())

    def _normalize_prompt_id(self, prompt_id: Optional[str]) -> Optional[str]:
        if prompt_id is None:
            return None
        trimmed = str(prompt_id).strip()
        return trimmed or None

    def _generate_id(self) -> str:
        return uuid.uuid4().hex

    def list_tasks(self) -> list[HumanTaskRecord]:
        with self._lock:
            records = list(self._records.values())
        return sorted(records, key=self._sort_key)

    def _sort_key(self, record: HumanTaskRecord) -> tuple[Any, ...]:
        return (
            0 if record.blocking else 1,
            STATUS_SORT_ORDER.get(record.status, 99),
            record.created_at,
        )

    def get_task(self, task_id: str) -> Optional[HumanTaskRecord]:
        with self._lock:
            return self._records.get(task_id)

    def create_task(
        self,
        title: str,
        description: str,
        *,
        project_id: Optional[str] = None,
        prompt_id: Optional[str] = None,
        blocking: bool | None = None,
        status: Optional[str] = None,
    ) -> HumanTaskRecord:
        clean_title = (title or "").strip()
        if not clean_title:
            raise ValueError("title is required")
        clean_description = (description or "").strip()
        now = utcnow_iso()
        record = HumanTaskRecord(
            task_id=self._generate_id(),
            title=clean_title,
            description=clean_description,
            status=self._normalize_status(status),
            blocking=bool(blocking),
            project_id=self._normalize_project_id(project_id),
            prompt_id=self._normalize_prompt_id(prompt_id),
            created_at=now,
            updated_at=now,
        )
        if record.status == HUMAN_TASK_STATUS_RESOLVED:
            record.resolved_at = now
        with self._lock:
            self._records[record.task_id] = record
            self._revision += 1
        self._persist()
        return record

    def update_task(self, task_id: str, **updates: Any) -> HumanTaskRecord:
        allowed_fields = {"title", "description", "status", "blocking", "project_id", "prompt_id"}
        invalid_keys = set(updates.keys()) - allowed_fields
        if invalid_keys:
            raise ValueError(f"Unsupported fields: {', '.join(sorted(invalid_keys))}")
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                raise KeyError(task_id)
            changed = False
            if "title" in updates:
                title = (updates["title"] or "").strip()
                if not title:
                    raise ValueError("title cannot be empty")
                if title != record.title:
                    record.title = title
                    changed = True
            if "description" in updates:
                description = (updates["description"] or "").strip()
                if description != record.description:
                    record.description = description
                    changed = True
            if "status" in updates:
                new_status = self._normalize_status(updates["status"])
                if new_status != record.status:
                    record.status = new_status
                    record.resolved_at = utcnow_iso() if new_status == HUMAN_TASK_STATUS_RESOLVED else None
                    changed = True
            if "blocking" in updates:
                blocking_value = bool(updates["blocking"])
                if blocking_value != record.blocking:
                    record.blocking = blocking_value
                    changed = True
            if "project_id" in updates:
                project_id = self._normalize_project_id(updates["project_id"])
                if project_id != record.project_id:
                    record.project_id = project_id
                    changed = True
            if "prompt_id" in updates:
                prompt_id = self._normalize_prompt_id(updates["prompt_id"])
                if prompt_id != record.prompt_id:
                    record.prompt_id = prompt_id
                    changed = True
            if changed:
                record.updated_at = utcnow_iso()
                self._revision += 1
        if changed:
            self._persist()
        return record

    def delete_task(self, task_id: str) -> HumanTaskRecord:
        with self._lock:
            record = self._records.pop(task_id, None)
            if record is None:
                raise KeyError(task_id)
            self._revision += 1
        self._persist()
        return record

    def health_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            records = list(self._records.values())
            revision = self._revision
        status_counts = Counter(record.status for record in records)
        blocking_records = [record for record in records if record.blocking]
        oldest_blocking = min(blocking_records, key=lambda rec: rec.created_at) if blocking_records else None
        payload: Dict[str, Any] = {
            "status_counts": dict(status_counts),
            "blocking_count": len(blocking_records),
            "total": len(records),
            "revision": revision,
        }
        if oldest_blocking:
            payload["oldest_blocking"] = {
                "task_id": oldest_blocking.task_id,
                "created_at": oldest_blocking.created_at,
                "age_seconds": seconds_since(oldest_blocking.created_at),
                "title": oldest_blocking.title,
                "project_id": oldest_blocking.project_id,
            }
        return payload

    def to_collection_payload(self, registry: Optional["ProjectRegistry"] = None) -> Dict[str, Any]:
        tasks = [build_human_task_payload(record, registry) for record in self.list_tasks()]
        summary = self.health_snapshot()
        payload = {
            "tasks": tasks,
            "summary": summary,
            "revision": summary.get("revision"),
        }
        return payload


def build_human_task_payload(record: HumanTaskRecord, registry: Optional["ProjectRegistry"]) -> Dict[str, Any]:
    payload = asdict(record)
    payload["age_seconds"] = seconds_since(record.created_at)
    if registry:
        project = registry.get(record.project_id)
        if project:
            payload["project"] = project.to_payload()
    return payload
