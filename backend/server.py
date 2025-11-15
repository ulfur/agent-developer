"""Agent-enabled development host backend server.

This lightweight HTTP server exposes a Task queue API and serves the
Vue/Vuetify frontend from the ../frontend directory. It is intentionally
implemented with the Python standard library so that it can run on a bare
Raspberry Pi OS Lite install without additional dependencies.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urlsplit

from auth import AuthManager, AuthenticatedUser
from eink.it8591 import IT8591Config, IT8951_ROTATE_180
# from git_branching import GitBranchError, PromptBranchDiscipline
from human_tasks import HumanTaskStore, build_human_task_payload
from log_utils import extract_stdout_preview
from preferences import PreferenceStore
from ssh_keys import SSHKeyManager, SSHKeyError


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
LOG_DIR = REPO_ROOT / "logs"
FRONTEND_DIR = REPO_ROOT / "frontend"
PROJECTS_DIR = REPO_ROOT / "projects"
PROMPT_DB_PATH = DATA_DIR / "prompts.json"
HUMAN_TASK_DB_PATH = DATA_DIR / "human_tasks.json"
GENERAL_LOG_PATH = LOG_DIR / "progress.log"
PREFERENCES_PATH = DATA_DIR / "preferences.json"
APP_CONTEXT: Dict[str, Any] = {}
PROMPT_DURATION_WINDOW = 50
TERMINAL_PROMPT_STATUSES: set[str] = {"completed", "failed", "canceled"}
PROMPT_STATUSES: tuple[str, ...] = ("queued", "running", "completed", "failed", "canceled")


def schedule_display_refresh(reason: str) -> None:
    """Request an e-ink refresh if the display manager is active."""
    manager = APP_CONTEXT.get("display_manager")
    if not manager:
        return
    try:
        manager.request_refresh(reason)
    except Exception:  # pragma: no cover - hardware path
        logger = APP_CONTEXT.get("audit_logger")
        if logger:
            logger.exception("Unable to enqueue display refresh (%s)", reason)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


def seconds_between(start_iso: Optional[str], end_iso: Optional[str]) -> Optional[float]:
    start_dt = parse_iso_timestamp(start_iso)
    end_dt = parse_iso_timestamp(end_iso)
    if not start_dt or not end_dt:
        return None
    delta = (end_dt - start_dt).total_seconds()
    return delta if delta >= 0 else 0.0


def seconds_since(timestamp: Optional[str]) -> Optional[float]:
    reference = parse_iso_timestamp(timestamp)
    if not reference:
        return None
    delta = (datetime.now(timezone.utc) - reference).total_seconds()
    return delta if delta >= 0 else 0.0


def ensure_dirs() -> None:
    for path in (DATA_DIR, LOG_DIR, FRONTEND_DIR, PROJECTS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_agents_context() -> str:
    agents_file = REPO_ROOT / "agents.md"
    if agents_file.exists():
        return agents_file.read_text(encoding="utf-8")
    return ""


@dataclass
class ProjectScope:
    description: str
    allow: list[str]
    deny: list[str]
    log_only: list[str]
    is_fallback: bool = False

    def to_payload(self) -> Dict[str, Any]:
        return {
            "description": self.description,
            "allow": list(self.allow),
            "deny": list(self.deny),
            "log_only": list(self.log_only),
            "is_fallback": self.is_fallback,
        }

    def guardrail_blurb(self) -> str:
        lines: list[str] = ["Scope guardrail:"]
        description = self.description.strip()
        if description:
            prefix = "Fallback manifest" if self.is_fallback else "Manifest"
            lines.append(f"- {prefix}: {description}")
        elif self.is_fallback:
            lines.append("- Fallback manifest: Only project files may be edited.")
        if self.allow:
            lines.append(f"- Allowed globs: {', '.join(self.allow)}")
        if self.deny:
            lines.append(f"- Denied globs: {', '.join(self.deny)}")
        if self.log_only:
            lines.append(f"- Log-only globs: {', '.join(self.log_only)}")
        return "\n".join(lines)


@dataclass
class ProjectDefinition:
    project_id: str
    name: str
    description: str
    context_file: Path | None
    guidance_file: Path | None
    launch_path: Optional[str] = None
    is_default: bool = False
    scope: ProjectScope | None = None
    root_dir: Path | None = None

    def read_context(self) -> str:
        if not self.context_file:
            return ""
        try:
            return self.context_file.read_text(encoding="utf-8")
        except OSError:
            return ""

    def read_guidance(self) -> str:
        if not self.guidance_file:
            return ""
        try:
            return self.guidance_file.read_text(encoding="utf-8")
        except OSError:
            return ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "id": self.project_id,
            "name": self.name,
            "description": self.description,
            "launch_url": self.launch_path,
        }


_SCOPE_LIST_KEYS = {"allow", "deny", "log_only"}


def _remove_inline_comment(value: str) -> str:
    trimmed = value
    for marker in (" #", "\t#"):
        marker_index = trimmed.find(marker)
        if marker_index != -1:
            trimmed = trimmed[:marker_index]
            break
    if trimmed.strip().startswith("#"):
        return ""
    return trimmed.rstrip()


def _strip_scope_value(value: str) -> str:
    trimmed = _remove_inline_comment(value).strip()
    if trimmed.startswith(("'", '"')) and trimmed.endswith(trimmed[0]):
        trimmed = trimmed[1:-1]
    return trimmed.strip()


def _split_inline_scope_list(value: str) -> list[str]:
    parts = []
    current = []
    depth = 0
    for char in value:
        if char == "," and depth == 0:
            entry = "".join(current).strip()
            if entry:
                parts.append(entry)
            current = []
            continue
        if char in {'\"', "'"}:
            depth ^= 1
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_scope_manifest(text: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "description": "",
        "allow": [],
        "deny": [],
        "log_only": [],
    }
    active_list: str | None = None
    capturing_description = False
    description_lines: list[str] = []

    def finalize_description() -> None:
        nonlocal capturing_description, description_lines
        if not capturing_description:
            return
        payload["description"] = "\n".join(line.rstrip() for line in description_lines).strip()
        capturing_description = False
        description_lines = []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0:
            finalize_description()
            active_list = None
            if ":" not in stripped:
                continue
            key, _, remainder = stripped.partition(":")
            key = key.strip()
            remainder = _remove_inline_comment(remainder.strip())
            if key == "description":
                if remainder and remainder not in {"|", ">"}:
                    payload["description"] = _strip_scope_value(remainder)
                else:
                    capturing_description = True
                    description_lines = []
                continue
            if key in _SCOPE_LIST_KEYS:
                payload[key] = []
                if not remainder or remainder in {"|", ">"}:
                    active_list = key
                    continue
                if remainder.startswith("[") and remainder.endswith("]"):
                    inner = remainder[1:-1].strip()
                    if inner:
                        for entry in _split_inline_scope_list(inner):
                            sanitized = _strip_scope_value(entry)
                            if sanitized:
                                payload[key].append(sanitized)
                else:
                    sanitized = _strip_scope_value(remainder)
                    if sanitized:
                        payload[key].append(sanitized)
                continue
            raise ValueError(f"Unknown key '{key}' in scope manifest")
        else:
            if capturing_description:
                description_lines.append(_remove_inline_comment(raw_line.lstrip()))
                continue
            if active_list:
                if stripped.startswith("- "):
                    sanitized_value = _strip_scope_value(stripped[2:])
                    if sanitized_value:
                        payload[active_list].append(sanitized_value)
                elif stripped == "-":
                    payload[active_list].append("")
                else:
                    if not payload[active_list]:
                        raise ValueError("Continuation encountered before first list item")
                    payload[active_list][-1] = f"{payload[active_list][-1]} {stripped}".strip()
                continue
            raise ValueError("Unexpected indentation in scope manifest")
    finalize_description()
    for key in _SCOPE_LIST_KEYS:
        payload[key] = [str(entry).strip() for entry in payload.get(key, []) if str(entry).strip()]
    payload["description"] = str(payload.get("description", "")).strip()
    return payload


class ProjectRegistry:
    def __init__(self, base_dir: Path, preferred_default: Optional[str] = None) -> None:
        self.base_dir = base_dir
        self._projects: dict[str, ProjectDefinition] = {}
        self._preferred_default = preferred_default
        self.default_project_id: Optional[str] = None
        self._logger = logging.getLogger("agent_backend.projects")
        self.reload()

    def reload(self) -> None:
        self._projects.clear()
        resolved_default: Optional[str] = None
        try:
            entries = sorted(
                [path for path in self.base_dir.iterdir() if path.is_dir()],
                key=lambda item: item.name.lower(),
            )
        except FileNotFoundError:
            entries = []
        for directory in entries:
            metadata = self._load_metadata(directory)
            project_id = (metadata.get("id") or directory.name).strip()
            if not project_id:
                continue
            name = (metadata.get("name") or project_id).strip()
            description = (metadata.get("description") or "").strip()
            context_filename = (metadata.get("contextFile") or metadata.get("context_file") or "context.md").strip()
            context_path = directory / context_filename if context_filename else None
            guidance_value = metadata.get("agentsFile") or metadata.get("guidanceFile") or metadata.get("agents_file")
            guidance_path: Path | None
            if guidance_value is not None:
                trimmed = str(guidance_value).strip()
                guidance_path = directory / trimmed if trimmed else None
            else:
                default_guidance = directory / "agents.md"
                guidance_path = default_guidance if default_guidance.exists() else None
            launch_path = metadata.get("launchPath") or metadata.get("launch_path") or metadata.get("launchUrl")
            is_default = bool(metadata.get("default"))
            scope = self._load_scope(directory, project_id)
            project = ProjectDefinition(
                project_id=project_id,
                name=name or project_id,
                description=description,
                context_file=context_path,
                guidance_file=guidance_path,
                launch_path=launch_path,
                is_default=is_default,
                scope=scope,
                root_dir=directory,
            )
            self._projects[project_id] = project
            if is_default:
                resolved_default = project_id
        if self._preferred_default and self._preferred_default in self._projects:
            resolved_default = self._preferred_default
        if not resolved_default and self._projects:
            resolved_default = next(iter(self._projects.keys()))
        self.default_project_id = resolved_default

    def _load_metadata(self, directory: Path) -> Dict[str, Any]:
        metadata_path = directory / "project.json"
        if not metadata_path.exists():
            return {}
        try:
            return json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _load_scope(self, directory: Path, project_id: str) -> ProjectScope | None:
        scope_path = directory / "scope.yml"
        if not scope_path.exists():
            return None
        try:
            raw = scope_path.read_text(encoding="utf-8")
        except OSError as exc:
            if self._logger:
                self._logger.warning("Unable to read scope manifest for %s: %s", project_id, exc)
            return None
        try:
            parsed = _parse_scope_manifest(raw)
        except ValueError as exc:
            if self._logger:
                self._logger.warning("Invalid scope manifest for %s: %s", project_id, exc)
            return None
        return ProjectScope(
            description=str(parsed.get("description", "")).strip(),
            allow=list(parsed.get("allow", [])),
            deny=list(parsed.get("deny", [])),
            log_only=list(parsed.get("log_only", [])),
        )

    def get(self, project_id: Optional[str]) -> Optional[ProjectDefinition]:
        if not project_id:
            return self._projects.get(self.default_project_id or "")
        return self._projects.get(project_id)

    def resolve_project_id(self, requested_id: Optional[str]) -> Optional[str]:
        if requested_id and requested_id in self._projects:
            return requested_id
        return self.default_project_id

    def to_payload(self) -> Dict[str, Any]:
        items: list[Dict[str, Any]] = []
        for project in self._projects.values():
            payload = project.to_payload()
            payload["scope"] = self.scope_payload(project)
            items.append(payload)
        return {
            "projects": items,
            "default_project_id": self.default_project_id,
        }

    def context_for(self, project_id: Optional[str]) -> str:
        project = self.get(project_id)
        if not project:
            return load_agents_context()
        base_context = load_agents_context().strip()
        project_context = project.read_context().strip()
        project_guidance = project.read_guidance().strip()
        header_lines = [f"Project focus: {project.name}"]
        if project.description:
            header_lines.append(project.description)
        sections = ["\n".join(header_lines)]
        scope = self._resolved_scope(project)
        guardrail = scope.guardrail_blurb().strip()
        if guardrail:
            sections.append(guardrail)
        if project_context:
            sections.append(project_context)
        if project_guidance:
            sections.append(project_guidance)
        if base_context:
            sections.append(f"Shared agent guidance:\n{base_context}")
        return "\n\n---\n\n".join(section.strip() for section in sections if section.strip()).strip()

    def _resolved_scope(self, project: ProjectDefinition) -> ProjectScope:
        if project.scope:
            return project.scope
        return self._default_scope_for(project)

    def _default_scope_for(self, project: ProjectDefinition) -> ProjectScope:
        root_dir = project.root_dir or (self.base_dir / project.project_id)
        try:
            relative = root_dir.relative_to(REPO_ROOT)
        except ValueError:
            relative = root_dir
        glob = f"{relative.as_posix()}/**"
        description = f"No scope.yml found; restrict edits to {glob} until a manifest is defined."
        return ProjectScope(
            description=description,
            allow=[glob],
            deny=[],
            log_only=[],
            is_fallback=True,
        )

    def _scope_payload_for(self, project: ProjectDefinition) -> Dict[str, Any]:
        return self._resolved_scope(project).to_payload()

    def scope_payload(self, project: ProjectDefinition) -> Dict[str, Any]:
        return self._scope_payload_for(project)

    def resolved_scope_for_id(self, project_id: Optional[str]) -> ProjectScope:
        project = self.get(project_id)
        if project:
            return self._resolved_scope(project)
        description = "No matching project scope; allow full repository until metadata is fixed."
        return ProjectScope(
            description=description,
            allow=["**"],
            deny=[],
            log_only=[],
            is_fallback=True,
        )


def build_prompt_context(project_id: Optional[str], registry: Optional[ProjectRegistry]) -> str:
    if registry:
        return registry.context_for(project_id)
    return load_agents_context()


ATTEMPT_HEADER_RE = re.compile(r"^Prompt received at (?P<ts>[^\n]+)", re.MULTILINE)
PROMPT_SECTION_RE = re.compile(r"---\s*(?P<body>.*?)(?:\nContext provided to Codex:|\Z)", re.DOTALL)
CONTEXT_SECTION_RE = re.compile(r"Context provided to Codex:\s*(?P<body>.*?)(?:\nCodex stdout:|\nCodex stderr:|\Z)", re.DOTALL)
STDOUT_SECTION_RE = re.compile(r"Codex stdout:\s*(?P<body>.*?)(?:\nCodex stderr:|\Z)", re.DOTALL)
STDERR_SECTION_RE = re.compile(r"Codex stderr:\s*(?P<body>.*)$", re.DOTALL)
ATTEMPT_STATUS_RE = re.compile(r"Attempt status:\s*(?P<status>\w+)", re.IGNORECASE)
ATTEMPT_COMPLETED_RE = re.compile(r"Attempt completed at (?P<ts>[^\n]+)")
ATTEMPT_DURATION_RE = re.compile(r"Elapsed seconds\s+(?P<seconds>[0-9.]+)")
SUMMARY_PARAGRAPH_COUNT = 2


def _extract_stdout_summary(stdout_text: str, paragraph_count: int = SUMMARY_PARAGRAPH_COUNT) -> str:
    """Return the trailing paragraphs from the Codex stdout section for use as a summary."""
    if not stdout_text:
        return ""
    trimmed = stdout_text.strip()
    if not trimmed:
        return ""
    paragraphs = [
        block.strip("\r\n")
        for block in re.split(r"(?:\r?\n){2,}", trimmed)
        if block.strip()
    ]
    if not paragraphs:
        return ""
    selected = paragraphs[-paragraph_count:] if paragraph_count > 0 else paragraphs
    return "\n\n".join(selected)


def _extract_metadata_summary(chunk: str, context_match: re.Match | None, header_match: re.Match) -> str:
    """Fall back to the first metadata line after the context if stdout has no useful content."""
    summary_text = ""
    end_of_context = context_match.end() if context_match else header_match.end()
    post_context = chunk[end_of_context:]
    for line in post_context.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Codex stdout:") or stripped.startswith("Codex stderr:"):
            continue
        summary_text = stripped
        break
    return summary_text


def parse_prompt_attempts(log_text: str) -> list[dict[str, str]]:
    attempts: list[dict[str, str]] = []
    if not log_text.strip():
        return attempts
    matches = list(ATTEMPT_HEADER_RE.finditer(log_text))
    if not matches:
        return attempts
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(log_text)
        chunk = log_text[start:end].strip()
        parsed = _parse_attempt_chunk(chunk)
        if parsed:
            attempts.append(parsed)
    return attempts


def _parse_attempt_chunk(chunk: str) -> dict[str, str] | None:
    if not chunk:
        return None
    header_match = ATTEMPT_HEADER_RE.match(chunk)
    if not header_match:
        return None
    received_at = header_match.group("ts").strip()
    prompt_match = PROMPT_SECTION_RE.search(chunk)
    prompt_text = prompt_match.group("body").strip() if prompt_match else ""
    context_match = CONTEXT_SECTION_RE.search(chunk)
    context_text = context_match.group("body").strip() if context_match else ""
    stdout_match = STDOUT_SECTION_RE.search(chunk)
    stdout_text = stdout_match.group("body").strip() if stdout_match else ""
    stderr_match = STDERR_SECTION_RE.search(chunk)
    stderr_text = stderr_match.group("body").strip() if stderr_match else ""
    status_match = ATTEMPT_STATUS_RE.search(chunk)
    completed_match = ATTEMPT_COMPLETED_RE.search(chunk)
    duration_match = ATTEMPT_DURATION_RE.search(chunk)

    summary_text = _extract_stdout_summary(stdout_text)
    if not summary_text:
        summary_text = _extract_metadata_summary(chunk, context_match, header_match)

    return {
        "received_at": received_at,
        "prompt_text": prompt_text,
        "context": context_text,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "summary": summary_text,
        "status": (status_match.group("status").strip().lower() if status_match else ""),
        "completed_at": completed_match.group("ts").strip() if completed_match else "",
        "duration_seconds": float(duration_match.group("seconds")) if duration_match else None,
    }


def build_prompt_payload(record: "PromptRecord", registry: Optional[ProjectRegistry] = None) -> Dict[str, Any]:
    """Return the full API payload for a single prompt record."""
    if registry is None:
        registry = APP_CONTEXT.get("projects")
    payload = asdict(record)
    log_path = Path(record.log_path)
    if log_path.exists():
        try:
            log_text = log_path.read_text(encoding="utf-8")
        except OSError:
            log_text = ""
    else:
        log_text = ""
    payload["log"] = log_text
    payload["attempt_logs"] = parse_prompt_attempts(log_text)
    payload["agents_context"] = build_prompt_context(record.project_id, registry)
    if registry:
        project = registry.get(record.project_id)
        if project:
            project_payload = project.to_payload()
            project_payload["scope"] = registry.scope_payload(project)
            payload["project"] = project_payload
    if record.status == "completed":
        payload["stdout_preview"] = extract_stdout_preview(record.log_path)
    else:
        payload["stdout_preview"] = ""
    return payload


@dataclass
class PromptRecord:
    prompt_id: str
    text: str
    status: str
    created_at: str
    updated_at: str
    enqueued_at: str
    log_path: str
    result_summary: Optional[str] = None
    attempts: int = 0
    project_id: Optional[str] = None
    started_at: Optional[str] = None
    current_wait_seconds: Optional[float] = None
    last_wait_seconds: Optional[float] = None
    last_run_seconds: Optional[float] = None
    last_finished_at: Optional[str] = None


class PromptStore:
    def __init__(self, db_path: Path, project_registry: Optional[ProjectRegistry] = None):
        self.db_path = db_path
        self.project_registry = project_registry
        self._lock = threading.Lock()
        self._pending: "queue.Queue[str]" = queue.Queue()
        self._records: Dict[str, PromptRecord] = {}
        self._stale_running: list[str] = []
        self._recovered_prompt_ids: list[str] = []
        self._logger = logging.getLogger("agent_backend")
        self._status_counts: Counter[str] = Counter()
        self._recent_durations: deque[tuple[Optional[float], Optional[float]]] = deque()
        self._recent_wait_sum = 0.0
        self._recent_run_sum = 0.0
        self._recent_wait_count = 0
        self._recent_run_count = 0
        self._duration_window = PROMPT_DURATION_WINDOW
        self._load()
        self._rebuild_duration_history()
        self._recover_inflight_prompts()

    def _load(self) -> None:
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
            self._status_counts.clear()
            for prompt_id, payload in data.items():
                if not isinstance(payload, dict):
                    continue
                payload.setdefault("attempts", 0)
                payload.pop("max_retries", None)
                status = str(payload.get("status") or "queued")
                payload["status"] = status
                payload["project_id"] = self._normalize_project_id(payload.get("project_id"))
                payload["enqueued_at"] = (
                    payload.get("enqueued_at")
                    or payload.get("created_at")
                    or utcnow_iso()
                )
                payload.setdefault("started_at", None)
                payload.setdefault("current_wait_seconds", None)
                payload.setdefault("last_wait_seconds", None)
                payload.setdefault("last_run_seconds", None)
                if status in TERMINAL_PROMPT_STATUSES:
                    payload["last_finished_at"] = payload.get("last_finished_at") or payload.get("updated_at")
                else:
                    payload["last_finished_at"] = payload.get("last_finished_at")
                record = PromptRecord(**payload)
                self._records[prompt_id] = record
                self._increment_status(record.status)
                if record.status == "queued":
                    self._pending.put(prompt_id)
                elif record.status == "running":
                    self._stale_running.append(prompt_id)

    def _persist(self) -> None:
        with self._lock:
            serialized = {pid: asdict(rec) for pid, rec in self._records.items()}
        self.db_path.write_text(json.dumps(serialized, indent=2) + "\n", encoding="utf-8")

    def _rebuild_duration_history(self) -> None:
        with self._lock:
            self._recent_durations.clear()
            self._recent_wait_sum = 0.0
            self._recent_run_sum = 0.0
            self._recent_wait_count = 0
            self._recent_run_count = 0
            finished_records = sorted(
                (
                    record
                    for record in self._records.values()
                    if record.last_finished_at and (record.last_wait_seconds is not None or record.last_run_seconds is not None)
                ),
                key=lambda record: record.last_finished_at,
            )
            for record in finished_records[-self._duration_window :]:
                self._append_duration_sample(record.last_wait_seconds, record.last_run_seconds)

    def _append_duration_sample(
        self,
        wait_seconds: Optional[float],
        run_seconds: Optional[float],
    ) -> None:
        while len(self._recent_durations) >= self._duration_window:
            old_wait, old_run = self._recent_durations.popleft()
            if old_wait is not None:
                self._recent_wait_sum -= old_wait
                self._recent_wait_count = max(0, self._recent_wait_count - 1)
            if old_run is not None:
                self._recent_run_sum -= old_run
                self._recent_run_count = max(0, self._recent_run_count - 1)
        self._recent_durations.append((wait_seconds, run_seconds))
        if wait_seconds is not None:
            self._recent_wait_sum += wait_seconds
            self._recent_wait_count += 1
        if run_seconds is not None:
            self._recent_run_sum += run_seconds
            self._recent_run_count += 1

    def _increment_status(self, status: str) -> None:
        self._status_counts[status] = self._status_counts.get(status, 0) + 1

    def _decrement_status(self, status: str) -> None:
        current = self._status_counts.get(status, 0) - 1
        if current <= 0:
            self._status_counts.pop(status, None)
        else:
            self._status_counts[status] = current

    def _change_status(self, record: PromptRecord, new_status: str) -> None:
        if record.status == new_status:
            return
        self._decrement_status(record.status)
        record.status = new_status
        self._increment_status(new_status)

    def _recover_inflight_prompts(self) -> None:
        if not self._stale_running:
            return
        recovered: list[str] = []
        for prompt_id in self._stale_running:
            record = self._records.get(prompt_id)
            if not record or record.status != "running":
                continue
            interrupted_at = utcnow_iso()
            summary = "Prompt interrupted when backend restarted; marked as failed"
            self._append_interrupted_attempt(record, summary, interrupted_at)
            self._update(prompt_id, status="failed", result_summary=summary, timestamp=interrupted_at)
            recovered.append(prompt_id)
            if self._logger:
                self._logger.warning(
                    "Recovered interrupted prompt %s; marked as failed", prompt_id
                )
        if recovered:
            self._recovered_prompt_ids.extend(recovered)
        self._stale_running.clear()

    def _append_interrupted_attempt(
        self, record: PromptRecord, summary: str, interrupted_at: str
    ) -> None:
        context_text = build_prompt_context(record.project_id, self.project_registry).strip() or "<context unavailable>"
        log_path = Path(record.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_lines = [
            f"Prompt received at {record.created_at}",
            "---",
            record.text,
            "",
            "Context provided to Codex:",
            context_text,
            "",
            summary,
            "Attempt status: failed",
            f"Attempt completed at {interrupted_at}",
            "Elapsed seconds 0.000",
            "Codex stdout:\n<no output captured>",
            "Codex stderr:\nPrompt run aborted when the backend restarted; please retry.",
        ]
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write("\n\n".join(log_lines))
            log_file.write("\n")

    def add_prompt(self, text: str, project_id: Optional[str] = None) -> PromptRecord:
        prompt_id = uuid.uuid4().hex
        log_path = str(LOG_DIR / f"prompt_{prompt_id}.log")
        normalized_project = self._normalize_project_id(project_id)
        now = utcnow_iso()
        record = PromptRecord(
            prompt_id=prompt_id,
            text=text,
            status="queued",
            created_at=now,
            updated_at=now,
            enqueued_at=now,
            log_path=log_path,
            project_id=normalized_project,
        )
        with self._lock:
            self._records[prompt_id] = record
            self._increment_status(record.status)
        self._pending.put(prompt_id)
        self._persist()
        return record

    def list_prompts(self) -> Dict[str, Any]:
        with self._lock:
            ordered = list(sorted(self._records.values(), key=lambda r: r.created_at, reverse=True))

        items: list[dict[str, Any]] = []
        for rec in ordered:
            payload = asdict(rec)
            if self.project_registry:
                project = self.project_registry.get(rec.project_id)
                if project:
                    payload["project"] = project.to_payload()
            if rec.status == "completed":
                payload["stdout_preview"] = extract_stdout_preview(rec.log_path)
            else:
                payload["stdout_preview"] = ""
            items.append(payload)
        return {"items": items}

    def get_prompt(self, prompt_id: str) -> Optional[PromptRecord]:
        with self._lock:
            return self._records.get(prompt_id)

    def pending_count(self) -> int:
        return self._pending.qsize()

    def status_counts(self) -> Dict[str, int]:
        with self._lock:
            return {status: self._status_counts.get(status, 0) for status in PROMPT_STATUSES}

    def oldest_prompt_info(self, status: str) -> Optional[Dict[str, Any]]:
        if status not in {"queued", "running"}:
            return None
        with self._lock:
            target: Optional[PromptRecord] = None
            target_timestamp: Optional[str] = None
            for record in self._records.values():
                if record.status != status:
                    continue
                timestamp = record.enqueued_at if status == "queued" else record.started_at
                if not timestamp:
                    continue
                if not target_timestamp or timestamp < target_timestamp:
                    target = record
                    target_timestamp = timestamp
            if not target or not target_timestamp:
                return None
        age = seconds_since(target_timestamp)
        payload = {
            "prompt_id": target.prompt_id,
            "timestamp": target_timestamp,
        }
        if age is not None:
            payload["age_seconds"] = age
        return payload

    def duration_stats(self) -> Dict[str, Any]:
        with self._lock:
            samples = list(self._recent_durations)
            wait_average = (
                self._recent_wait_sum / self._recent_wait_count if self._recent_wait_count else None
            )
            run_average = (
                self._recent_run_sum / self._recent_run_count if self._recent_run_count else None
            )
            wait_values = [wait for wait, _ in samples if wait is not None]
            run_values = [run for _, run in samples if run is not None]
        wait_max = max(wait_values) if wait_values else None
        run_max = max(run_values) if run_values else None
        return {
            "window": self._duration_window,
            "samples": len(samples),
            "wait": {
                "average": wait_average,
                "max": wait_max,
                "count": len(wait_values),
            },
            "run": {
                "average": run_average,
                "max": run_max,
                "count": len(run_values),
            },
        }

    def health_snapshot(self) -> Dict[str, Any]:
        status_counts = self.status_counts()
        return {
            "status_counts": status_counts,
            "oldest": {
                "queued": self.oldest_prompt_info("queued"),
                "running": self.oldest_prompt_info("running"),
            },
            "durations": self.duration_stats(),
        }

    def begin_attempt(self, prompt_id: str) -> PromptRecord:
        with self._lock:
            record = self._records[prompt_id]
            start_time = utcnow_iso()
            wait_seconds = seconds_between(record.enqueued_at, start_time)
            record.attempts += 1
            self._change_status(record, "running")
            record.started_at = start_time
            record.current_wait_seconds = wait_seconds
            record.updated_at = start_time
        self._persist()
        return record

    def _normalize_project_id(self, project_id: Optional[str]) -> Optional[str]:
        if not self.project_registry:
            return project_id
        return self.project_registry.resolve_project_id(project_id)
        return record

    def mark_completed(self, prompt_id: str, summary: str) -> None:
        self._update(prompt_id, status="completed", result_summary=summary)

    def mark_failed(self, prompt_id: str, summary: str) -> None:
        self._update(prompt_id, status="failed", result_summary=summary)

    def mark_canceled(self, prompt_id: str, summary: str) -> None:
        self._update(prompt_id, status="canceled", result_summary=summary)

    def retry_prompt(self, prompt_id: str) -> PromptRecord:
        with self._lock:
            record = self._records.get(prompt_id)
            if record is None:
                raise KeyError(prompt_id)
            if record.status == "running":
                raise ValueError("prompt still running")
            now = utcnow_iso()
            self._change_status(record, "queued")
            record.enqueued_at = now
            record.started_at = None
            record.current_wait_seconds = None
            record.updated_at = now
        self._pending.put(prompt_id)
        self._persist()
        return record

    def update_prompt_text(self, prompt_id: str, text: str) -> PromptRecord:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("prompt text cannot be empty")
        with self._lock:
            record = self._records.get(prompt_id)
            if record is None:
                raise KeyError(prompt_id)
            if record.status not in {"queued", "failed", "completed", "canceled"}:
                raise ValueError("cannot edit prompt while running")
            record.text = clean_text
            record.updated_at = utcnow_iso()
        self._persist()
        return record

    def edit_prompt(self, prompt_id: str, new_text: str) -> PromptRecord:
        normalized = (new_text or "").strip()
        if not normalized:
            raise ValueError("prompt text is required")
        with self._lock:
            record = self._records.get(prompt_id)
            if record is None:
                raise KeyError(prompt_id)
            if record.status != "queued":
                raise ValueError("prompt can only be edited while queued")
            record.text = normalized
            record.updated_at = utcnow_iso()
        self._persist()
        return record

    def consume_recovered_prompts(self) -> List[str]:
        with self._lock:
            recovered = list(self._recovered_prompt_ids)
            self._recovered_prompt_ids.clear()
        return recovered

    def delete_prompt(self, prompt_id: str) -> PromptRecord:
        with self._lock:
            record = self._records.get(prompt_id)
            if record is None:
                raise KeyError(prompt_id)
            if record.status != "queued":
                raise ValueError("prompt can only be deleted while queued")
            removed = self._records.pop(prompt_id)
            self._decrement_status(removed.status)
        self._persist()
        log_path = Path(removed.log_path)
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass
        return removed

    def _update(self, prompt_id: str, *, timestamp: Optional[str] = None, **updates: Any) -> None:
        with self._lock:
            record = self._records[prompt_id]
            now = timestamp or utcnow_iso()
            new_status = updates.get("status")
            if new_status:
                previous_status = record.status
                self._change_status(record, new_status)
                if new_status == "queued":
                    record.enqueued_at = now
                    record.started_at = None
                    record.current_wait_seconds = None
                elif new_status == "running":
                    record.started_at = now
                    record.current_wait_seconds = None
                elif previous_status == "running":
                    wait_seconds = record.current_wait_seconds
                    run_seconds = seconds_between(record.started_at, now)
                    record.last_wait_seconds = wait_seconds
                    record.last_run_seconds = run_seconds
                    record.last_finished_at = now
                    record.started_at = None
                    record.current_wait_seconds = None
                    self._append_duration_sample(wait_seconds, run_seconds)
            for key, value in updates.items():
                if key == "status":
                    continue
                setattr(record, key, value)
            if new_status in TERMINAL_PROMPT_STATUSES:
                record.last_finished_at = record.last_finished_at or now
            record.updated_at = now
        self._persist()

    def next_prompt_id(self, timeout: float = 1.0) -> Optional[str]:
        try:
            return self._pending.get(timeout=timeout)
        except queue.Empty:
            return None


class CodexRunner:
    def __init__(
        self,
        repo_root: Path,
        streamer: Optional["EventStreamer"] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.repo_root = repo_root
        self.codex_bin = os.environ.get("CODEX_CLI", "codex")
        self.sandbox_mode = os.environ.get("CODEX_SANDBOX")
        self.streamer = streamer
        self.logger = logger or logging.getLogger("codex_runner")
        self._lock = threading.Lock()
        self._active_prompt_id: Optional[str] = None
        self._active_process: Optional[subprocess.Popen[str]] = None
        self._cancel_target: Optional[str] = None
        self._cancel_summary: str = ""

    def arm_prompt(self, prompt_id: str) -> None:
        with self._lock:
            self._active_prompt_id = prompt_id

    def cancel(self, prompt_id: str, summary: str = "Prompt canceled by user") -> bool:
        with self._lock:
            if self._active_prompt_id != prompt_id:
                return False
            self._cancel_target = prompt_id
            self._cancel_summary = summary
            process = self._active_process
        if process and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        return True

    def run(
        self,
        prompt_id: str,
        prompt_text: str,
        context_text: str,
        log_path: Path,
        *,
        project_id: Optional[str] = None,
        scope: Optional[ProjectScope] = None,
    ) -> tuple[str, bool, bool]:
        base_cmd = [self.codex_bin, "exec", "--skip-git-repo-check"]
        if self.sandbox_mode:
            base_cmd.extend(["--sandbox", self.sandbox_mode])
        base_cmd.append("-")
        guard_script = REPO_ROOT / "scope_guard.py"
        if guard_script.exists():
            cmd = [sys.executable, str(guard_script), *base_cmd]
        else:
            cmd = base_cmd
        received_at = utcnow_iso()
        header = [
            f"Prompt received at {received_at}",
            "---",
            prompt_text,
            "",
            "Context provided to Codex:",
            context_text.strip() or "<context unavailable>",
            "",
        ]
        log_lines = ["\n".join(header)]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        git_session = None
        git_notes: list[str] = []
        git_setup_error: Optional[str] = None
        scope_payload = scope.to_payload() if scope else {
            "description": "Scope guard fallback: allow entire repository",
            "allow": ["**"],
            "deny": [],
            "log_only": [],
            "is_fallback": True,
        }
        scope_status_path = LOG_DIR / f"scope_guard_{prompt_id}.json"
        try:
            scope_status_path.unlink(missing_ok=True)
        except OSError:
            pass
        env = os.environ.copy()
        env.update(
            {
                "CODEX_SCOPE_MANIFEST": json.dumps(scope_payload),
                "CODEX_SCOPE_PROMPT_ID": prompt_id,
                "CODEX_SCOPE_PROJECT_ID": project_id or "",
                "CODEX_SCOPE_STATUS_PATH": str(scope_status_path),
                "CODEX_SCOPE_VIOLATION_LOG": str(LOG_DIR / "scope_violations.log"),
                "CODEX_SCOPE_REPO_ROOT": str(self.repo_root),
            }
        )
        stdout_buffer: list[str] = []
        stderr_buffer: list[str] = []
        success = True
        summary = "Codex run succeeded"
        start_time = time.perf_counter()
        process: Optional[subprocess.Popen[str]] = None

        def _stream_entry(label: str, content: str) -> str:
            body = (content or "").rstrip()
            return f"{label}:\n{body if body else '<no output>'}"

        def _pump(stream: Any, buffer: list[str], stream_name: str) -> None:
            if stream is None:
                return
            try:
                for chunk in iter(stream.readline, ""):
                    if not chunk:
                        continue
                    buffer.append(chunk)
                    self._broadcast_stream(prompt_id, stream_name, chunk)
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        self._broadcast_stream(prompt_id, "stdout", "", reset=True)
        self._broadcast_stream(prompt_id, "stderr", "", reset=True)

        with self._lock:
            self._active_prompt_id = prompt_id
            skip_execution = self._cancel_target == prompt_id
            pending_summary = self._cancel_summary if skip_execution else ""

        if not skip_execution:
            try:
                git_session = self.branch_discipline.begin_run(prompt_id, prompt_text)
                if git_session and git_session.notes:
                    git_notes.extend(git_session.notes)
            except GitBranchError as exc:
                git_setup_error = str(exc)
                git_notes.append(f"Git branch preparation failed: {git_setup_error}")

        if not skip_execution and git_setup_error is None:
            try:
                process = subprocess.Popen(
                    cmd,
                    cwd=self.repo_root,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    env=env,
                )
                with self._lock:
                    self._active_process = process
                assert process.stdin is not None
                process.stdin.write(prompt_text)
                process.stdin.close()

                stdout_thread = threading.Thread(
                    target=_pump, args=(process.stdout, stdout_buffer, "stdout"), daemon=True
                )
                stderr_thread = threading.Thread(
                    target=_pump, args=(process.stderr, stderr_buffer, "stderr"), daemon=True
                )
                stdout_thread.start()
                stderr_thread.start()
                return_code = process.wait()
                stdout_thread.join()
                stderr_thread.join()
                if return_code != 0:
                    success = False
                    summary = f"Codex failed with exit code {return_code}"
            except FileNotFoundError:
                success = False
                summary = "Codex CLI not found; logged placeholder output"
                self._broadcast_stream(prompt_id, "stderr", summary + "\n", reset=True)
            except Exception as exc:  # pragma: no cover - defensive
                success = False
                summary = f"Codex invocation error: {exc}"
                self._broadcast_stream(prompt_id, "stderr", f"{summary}\n", reset=True)
            finally:
                with self._lock:
                    self._active_process = None
                if process and process.poll() is None:
                    try:
                        process.kill()
                    except Exception:
                        pass
        else:
            success = False
            if skip_execution:
                summary = pending_summary or "Codex run canceled before execution"
            else:
                summary = f"Git branch preparation failed: {git_setup_error}"
                self._broadcast_stream(prompt_id, "stderr", summary + "\n", reset=True)

        stdout_text = "".join(stdout_buffer)
        stderr_text = "".join(stderr_buffer)
        violation_summary = ""
        if scope_status_path.exists():
            try:
                payload = json.loads(scope_status_path.read_text(encoding="utf-8"))
                violation_summary = str(payload.get("message") or "").strip()
            except (OSError, json.JSONDecodeError):
                violation_summary = "Scope guard violation detected"
            finally:
                try:
                    scope_status_path.unlink(missing_ok=True)
                except OSError:
                    pass
        elapsed_seconds = time.perf_counter() - start_time
        completed_at = utcnow_iso()
        cleanup_error: Optional[str] = None
        if git_session:
            try:
                cleanup_notes = self.branch_discipline.finalize_run(git_session)
                if cleanup_notes:
                    git_notes.extend(cleanup_notes)
            except GitBranchError as exc:
                cleanup_error = str(exc)
                git_notes.append(f"Git cleanup blocked: {cleanup_error}")

        with self._lock:
            canceled = self._cancel_target == prompt_id
            cancel_summary = self._cancel_summary
            if canceled:
                self._cancel_target = None
                self._cancel_summary = ""
            self._active_prompt_id = None

        if canceled:
            success = False
            summary = cancel_summary or summary or "Prompt canceled by user"
        elif violation_summary:
            success = False
            summary = violation_summary
        if cleanup_error:
            success = False
            if canceled:
                summary = f"{summary}; cleanup failed: {cleanup_error}"
            else:
                summary = f"Prompt branch cleanup failed: {cleanup_error}"

        attempt_status = "canceled" if canceled else ("completed" if success else "failed")

        log_lines.append(summary)
        if git_notes:
            git_section = "\n".join(f"- {note}" for note in git_notes)
            log_lines.append(f"Git workflow notes:\n{git_section}")
        log_lines.append(f"Attempt status: {attempt_status}")
        log_lines.append(f"Attempt completed at {completed_at}")
        log_lines.append(f"Elapsed seconds {elapsed_seconds:.3f}")
        log_lines.append(_stream_entry("Codex stdout", stdout_text))
        log_lines.append(_stream_entry("Codex stderr", stderr_text))

        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write("\n\n".join(log_lines))
            log_file.write("\n")

        self._broadcast_stream(prompt_id, "stdout", "", done=True)
        self._broadcast_stream(prompt_id, "stderr", "", done=True)

        return summary, success, canceled

    def _broadcast_stream(
        self,
        prompt_id: str,
        stream_name: str,
        chunk: str,
        *,
        reset: bool = False,
        done: bool = False,
    ) -> None:
        if not self.streamer:
            return
        payload = {
            "prompt_id": prompt_id,
            "stream": stream_name,
            "chunk": chunk,
            "reset": reset,
            "done": done,
            "timestamp": utcnow_iso(),
        }
        self.streamer.broadcast_stream(payload)


class PromptWorker(threading.Thread):
    def __init__(
        self,
        store: PromptStore,
        runner: CodexRunner,
        logger: logging.Logger,
        display_manager: Optional["TaskQueueDisplayManager"] = None,
        event_streamer: Optional["EventStreamer"] = None,
    ):
        super().__init__(daemon=True)
        self.store = store
        self.runner = runner
        self.logger = logger
        self._stop_event = threading.Event()
        self.display_manager = display_manager
        self.event_streamer = event_streamer
        self._current_lock = threading.Lock()
        self._current_prompt_id: Optional[str] = None
        self._restart_requests: set[str] = set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            prompt_id = self.store.next_prompt_id()
            if not prompt_id:
                continue
            record = self.store.get_prompt(prompt_id)
            if not record:
                continue
            self.runner.arm_prompt(prompt_id)
            with self._current_lock:
                self._current_prompt_id = prompt_id
            record = self.store.begin_attempt(prompt_id)
            self._notify_display("running")
            self._emit_updates(prompt_id)
            log_path = Path(record.log_path)
            self.logger.info("Processing prompt %s", prompt_id)
            try:
                context_text = build_prompt_context(record.project_id, self.store.project_registry)
                project_scope = None
                if self.store.project_registry:
                    project_scope = self.store.project_registry.resolved_scope_for_id(record.project_id)
                summary, success, canceled = self.runner.run(
                    prompt_id,
                    record.text,
                    context_text,
                    log_path,
                    project_id=record.project_id,
                    scope=project_scope,
                )
            finally:
                with self._current_lock:
                    self._current_prompt_id = None
            if canceled:
                restart_requested = self._consume_restart_request(prompt_id)
                self.store.mark_canceled(prompt_id, summary)
                self.logger.info("Prompt %s canceled", prompt_id)
                self._notify_display("canceled")
                if restart_requested:
                    try:
                        self.store.retry_prompt(prompt_id)
                        self.logger.info("Prompt %s re-queued after cancellation", prompt_id)
                        self._notify_display("queued")
                    except ValueError:
                        self.logger.warning("Prompt %s could not be re-queued after cancellation", prompt_id)
            elif success:
                self.store.mark_completed(prompt_id, summary)
                self.logger.info("Prompt %s completed", prompt_id)
                self._notify_display("completed")
                self._clear_restart_request(prompt_id)
            else:
                self.store.mark_failed(prompt_id, summary)
                self.logger.error("Prompt %s failed: %s", prompt_id, summary)
                self._notify_display("failed")
                self._clear_restart_request(prompt_id)
            self._emit_updates(prompt_id)

    def stop(self) -> None:
        self._stop_event.set()

    def request_cancel(self, prompt_id: str, *, restart: bool = False) -> bool:
        summary = "Prompt canceled by operator"
        if restart:
            summary = "Prompt canceled; restart requested"
        with self._current_lock:
            if self._current_prompt_id != prompt_id:
                return False
            if restart:
                self._restart_requests.add(prompt_id)
            else:
                self._restart_requests.discard(prompt_id)
        canceled = self.runner.cancel(prompt_id, summary)
        if not canceled and restart:
            with self._current_lock:
                self._restart_requests.discard(prompt_id)
        return canceled

    def _consume_restart_request(self, prompt_id: str) -> bool:
        with self._current_lock:
            if prompt_id in self._restart_requests:
                self._restart_requests.remove(prompt_id)
                return True
            return False

    def _clear_restart_request(self, prompt_id: str) -> None:
        with self._current_lock:
            self._restart_requests.discard(prompt_id)

    def _notify_display(self, reason: str) -> None:
        if self.display_manager:
            try:
                self.display_manager.request_refresh(reason)
            except Exception:
                self.logger.exception("Unable to enqueue display refresh")

    def _emit_updates(self, prompt_id: str) -> None:
        if not self.event_streamer:
            return
        self.event_streamer.broadcast_queue()
        self.event_streamer.broadcast_prompt(prompt_id)
        self.event_streamer.broadcast_health()


class WebSocketConnection:
    """Minimal WebSocket implementation tailored for server-side pushes."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, handler: "AgentHTTPRequestHandler", manager: "WebSocketManager"):
        self.handler = handler
        self.manager = manager
        self.user: AuthenticatedUser | None = None
        self.alive = False
        self._send_lock = threading.Lock()
        raw_socket = getattr(handler, "request", None)
        if raw_socket is None:
            raw_socket = handler.connection
        self._socket: socket.socket = raw_socket

    def serve(self) -> None:
        if not self._perform_handshake():
            return
        self.alive = True
        self._socket.settimeout(1.0)
        self.manager.register(self)
        self.send_json("hello", {"timestamp": utcnow_iso()})
        try:
            while self.alive:
                try:
                    frame = self._read_frame()
                except (socket.timeout, TimeoutError):
                    # Buffered readers raise TimeoutError when the underlying socket
                    # hits its timeout; keep the connection alive and poll again.
                    continue
                except ConnectionError:
                    break
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:  # close
                    self.close()
                    break
                if opcode == 0x9:  # ping
                    self._send_frame(0xA, payload)  # pong
                    continue
                if opcode == 0x1:  # text
                    self._handle_text(payload)
        finally:
            self.alive = False
            self.manager.unregister(self)
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.handler.close_connection = True

    # ------------------------------------------------------------------ helpers
    def _perform_handshake(self) -> bool:
        key = self.handler.headers.get("Sec-WebSocket-Key")
        if not key:
            self.handler.send_error(HTTPStatus.BAD_REQUEST, "Missing Sec-WebSocket-Key")
            return False
        accept = base64.b64encode(hashlib.sha1((key + self.GUID).encode("ascii")).digest()).decode("ascii")
        self.handler.send_response(101, "Switching Protocols")
        self.handler.send_header("Upgrade", "websocket")
        self.handler.send_header("Connection", "Upgrade")
        self.handler.send_header("Sec-WebSocket-Accept", accept)
        self.handler.end_headers()
        self.handler.close_connection = False
        return True

    def _read_buffer(self, size: int) -> bytes:
        """Read raw bytes from the socket without relying on buffered file objects.

        The default `rfile` becomes unusable once a timeout occurs (it raises
        `OSError: cannot read from timed out object` forever), so bypass it and
        read directly from the underlying socket which tolerates repeated
        timeouts.
        """
        try:
            return self._socket.recv(size)
        except (socket.timeout, TimeoutError):
            raise
        except OSError as exc:
            if self._is_timeout_oserror(exc):
                raise TimeoutError("socket read timed out") from exc
            raise

    @staticmethod
    def _is_timeout_oserror(exc: OSError) -> bool:
        errno_value = getattr(exc, "errno", None)
        if errno_value in {errno.EAGAIN, errno.EWOULDBLOCK, errno.ETIMEDOUT}:
            return True
        message = str(exc).lower()
        return "timed out" in message or "timeout" in message

    def _read_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            data = self._read_buffer(size - len(chunks))
            if not data:
                raise ConnectionError("unexpected EOF while reading WebSocket frame")
            chunks.extend(data)
        return bytes(chunks)

    def _read_frame(self) -> tuple[int, bytes] | None:
        try:
            header = self._read_exact(2)
        except ConnectionError:
            return None
        byte1, byte2 = header
        fin = byte1 & 0x80
        opcode = byte1 & 0x0F
        if not fin:
            raise ConnectionError("fragmented frames are unsupported")
        masked = byte2 & 0x80
        length = byte2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        if masked:
            mask = self._read_exact(4)
        else:
            mask = b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> bool:
        if not self.alive:
            return False
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < (1 << 16):
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))
        message = bytes(header) + payload
        try:
            with self._send_lock:
                self.handler.wfile.write(message)
                self.handler.wfile.flush()
            return True
        except OSError:
            self.alive = False
            return False

    def _handle_text(self, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_json("error", {"message": "invalid JSON payload"})
            return
        if not isinstance(message, dict):
            self.send_json("error", {"message": "payload must be an object"})
            return
        self.manager.handle_client_message(self, message)

    def send_json(self, event_type: str, payload: Dict[str, Any]) -> bool:
        envelope = json.dumps({"type": event_type, "payload": payload}, ensure_ascii=False).encode("utf-8")
        return self._send_frame(0x1, envelope)

    def send_raw_text(self, payload: bytes) -> bool:
        return self._send_frame(0x1, payload)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.alive:
            return
        close_payload = struct.pack(">H", code) + reason.encode("utf-8")
        self._send_frame(0x8, close_payload)
        self.alive = False


class WebSocketManager:
    """Tracks connected WebSocket clients and handles routing."""

    def __init__(self, auth_manager: AuthManager, logger: logging.Logger):
        self.auth_manager = auth_manager
        self.logger = logger
        self._clients: set[WebSocketConnection] = set()
        self._lock = threading.Lock()
        self.event_streamer: EventStreamer | None = None

    def register(self, connection: WebSocketConnection) -> None:
        with self._lock:
            self._clients.add(connection)
        self.logger.info("WebSocket client connected (%s total)", len(self._clients))

    def unregister(self, connection: WebSocketConnection) -> None:
        with self._lock:
            self._clients.discard(connection)
        self.logger.info("WebSocket client disconnected (%s total)", len(self._clients))

    def broadcast(
        self,
        event_type: str,
        payload: Dict[str, Any],
        targets: Optional[Iterable[WebSocketConnection]] = None,
    ) -> None:
        if targets is None:
            with self._lock:
                recipients = list(self._clients)
        else:
            recipients = list(targets)
        if not recipients:
            return
        message = json.dumps({"type": event_type, "payload": payload}, ensure_ascii=False).encode("utf-8")
        dead: List[WebSocketConnection] = []
        for connection in recipients:
            if not connection.alive:
                dead.append(connection)
                continue
            if connection.user is None:
                continue
            if not connection.send_raw_text(message):
                dead.append(connection)
        for stale in dead:
            self.unregister(stale)

    def handle_client_message(self, connection: WebSocketConnection, payload: Dict[str, Any]) -> None:
        message_type = (payload.get("type") or "").strip().lower()
        if message_type == "auth":
            token = (payload.get("token") or "").strip()
            if not token:
                connection.send_json("error", {"message": "auth token required"})
                return
            user = self.auth_manager.verify_token(token)
            if not user:
                connection.send_json("error", {"message": "invalid or expired token"})
                connection.close(4003, "auth failed")
                return
            connection.user = user
            connection.send_json("auth_ok", {"user": self.auth_manager.user_payload(user)})
            if self.event_streamer:
                self.event_streamer.send_initial_state(connection)
            return

        if connection.user is None:
            connection.send_json("error", {"message": "authentication required"})
            return

        if message_type == "fetch_prompt":
            prompt_id = (payload.get("prompt_id") or "").strip()
            if not prompt_id:
                connection.send_json("error", {"message": "prompt_id is required"})
                return
            if self.event_streamer:
                self.event_streamer.broadcast_prompt(prompt_id, targets=[connection])
            return

        if message_type == "request_queue":
            if self.event_streamer:
                self.event_streamer.broadcast_queue(targets=[connection])
            return

        if message_type == "ping":
            connection.send_json("pong", {"timestamp": utcnow_iso()})
            return

        connection.send_json("error", {"message": f"unknown message type: {message_type or '<missing>'}"})


class EventStreamer:
    """Bridges backend state changes to WebSocket clients."""

    def __init__(
        self,
        store: PromptStore,
        logger: logging.Logger,
        ws_manager: WebSocketManager,
        project_registry: Optional[ProjectRegistry] = None,
        human_tasks: Optional[HumanTaskStore] = None,
    ):
        self.store = store
        self.logger = logger
        self.ws_manager = ws_manager
        self.project_registry = project_registry
        self.human_tasks = human_tasks

    def broadcast_queue(self, targets: Optional[Iterable[WebSocketConnection]] = None) -> None:
        snapshot = self.store.list_prompts()
        self.ws_manager.broadcast("queue_snapshot", snapshot, targets=targets)

    def broadcast_prompt(
        self,
        prompt_id: str,
        targets: Optional[Iterable[WebSocketConnection]] = None,
    ) -> None:
        record = self.store.get_prompt(prompt_id)
        if not record:
            return
        payload = {"prompt": build_prompt_payload(record, self.project_registry)}
        self.ws_manager.broadcast("prompt_update", payload, targets=targets)

    def broadcast_prompt_deleted(
        self,
        prompt_id: str,
        targets: Optional[Iterable[WebSocketConnection]] = None,
    ) -> None:
        payload = {"prompt_id": prompt_id}
        self.ws_manager.broadcast("prompt_deleted", payload, targets=targets)

    def broadcast_health(self, targets: Optional[Iterable[WebSocketConnection]] = None) -> None:
        prompt_metrics = self.store.health_snapshot()
        task_metrics = self.human_tasks.health_snapshot() if self.human_tasks else {}
        payload = {
            "status": "ok",
            "timestamp": utcnow_iso(),
            "pending": prompt_metrics.get("status_counts", {}).get("queued", 0),
            "metrics": {
                **prompt_metrics,
                "human_tasks": task_metrics,
            },
        }
        self.ws_manager.broadcast("health", payload, targets=targets)

    def send_initial_state(self, connection: WebSocketConnection) -> None:
        self.broadcast_queue(targets=[connection])
        self.broadcast_health(targets=[connection])

    def broadcast_stream(self, payload: Dict[str, Any]) -> None:
        if "prompt_id" not in payload:
            return
        self.ws_manager.broadcast("prompt_stream", payload)


class HealthBroadcaster(threading.Thread):
    """Periodic health snapshot publisher."""

    def __init__(self, streamer: EventStreamer, interval_seconds: int = 10):
        super().__init__(daemon=True)
        self.streamer = streamer
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            self.streamer.broadcast_health()
            self._stop.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop.set()


class AgentHTTPRequestHandler(SimpleHTTPRequestHandler):
    server_version = "AgentDevServer/0.1"

    def __init__(self, *args: Any, directory: Optional[str] = None, **kwargs: Any) -> None:
        self.current_user: Optional[AuthenticatedUser] = None
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def translate_path(self, path: str) -> str:
        return super().translate_path(path)

    def _set_common_headers(self, status: int = 200, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self) -> None:  # noqa: N802 (http method name)
        if self.path.startswith("/api/"):
            self._set_common_headers()
            self.end_headers()
        else:
            super().do_OPTIONS()

    def do_PUT(self) -> None:  # noqa: N802
        if self.path.startswith("/api/"):
            self._handle_api_put()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "PUT not supported for static assets")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ws":
            self._handle_websocket()
        elif self.path.startswith("/api/"):
            self._handle_api_get()
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/api/"):
            self._handle_api_post()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "POST not supported for static assets")

    def do_PUT(self) -> None:  # noqa: N802
        if self.path.startswith("/api/"):
            self._handle_api_put()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "PUT not supported for static assets")

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path.startswith("/api/"):
            self._handle_api_delete()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "DELETE not supported for static assets")

    # API helpers -----------------------------------------------------
    def _handle_api_get(self) -> None:
        if self.path == "/api/projects":
            registry: Optional[ProjectRegistry] = APP_CONTEXT.get("projects")
            if registry:
                payload = registry.to_payload()
            else:
                payload = {"projects": [], "default_project_id": None}
            self._write_json(payload)
            return
        if not self._require_auth():
            return
        if self.path == "/api/health":
            store: PromptStore = APP_CONTEXT["store"]
            metrics = store.health_snapshot()
            human_store: Optional[HumanTaskStore] = APP_CONTEXT.get("human_tasks")
            human_metrics = human_store.health_snapshot() if human_store else {}
            payload = {
                "status": "ok",
                "timestamp": utcnow_iso(),
                "pending": metrics.get("status_counts", {}).get("queued", 0),
                "metrics": {**metrics, "human_tasks": human_metrics},
                "user": APP_CONTEXT["auth"].user_payload(self.current_user) if self.current_user else None,
            }
            self._write_json(payload)
        elif self.path == "/api/prompts":
            payload = APP_CONTEXT["store"].list_prompts()
            self._write_json(payload)
        elif self.path.startswith("/api/prompts/"):
            prompt_id = self.path.rsplit("/", 1)[-1]
            record = APP_CONTEXT["store"].get_prompt(prompt_id)
            if not record:
                self._write_json({"error": "prompt not found"}, status=404)
            else:
                registry: Optional[ProjectRegistry] = APP_CONTEXT.get("projects")
                self._write_json(build_prompt_payload(record, registry))
        elif self.path == "/api/human_tasks":
            store: HumanTaskStore = APP_CONTEXT["human_tasks"]
            registry = APP_CONTEXT.get("projects")
            self._write_json(store.to_collection_payload(registry))
        elif self.path.startswith("/api/human_tasks/"):
            task_id = self.path.rsplit("/", 1)[-1]
            store = APP_CONTEXT["human_tasks"]
            record = store.get_task(task_id)
            if not record:
                self._write_json({"error": "human task not found"}, status=404)
            else:
                registry = APP_CONTEXT.get("projects")
                self._write_json({"task": build_human_task_payload(record, registry)})
        elif self.path == "/api/logs":
            if GENERAL_LOG_PATH.exists():
                content = GENERAL_LOG_PATH.read_text(encoding="utf-8")
            else:
                content = ""
            self._write_json({"log": content})
        elif self.path == "/api/user/ssh_keys":
            manager: Optional[SSHKeyManager] = APP_CONTEXT.get("ssh_keys")
            if not manager:
                self._write_json({"keys": []})
                return
            try:
                keys = manager.list_public_keys()
            except SSHKeyError as exc:
                APP_CONTEXT["audit_logger"].error("Unable to load SSH keys: %s", exc)
                self._write_json({"error": str(exc)}, status=500)
                return
            self._write_json({"keys": keys})
        else:
            self._write_json({"error": "unknown endpoint"}, status=404)

    def _handle_websocket(self) -> None:
        manager: Optional[WebSocketManager] = APP_CONTEXT.get("ws_manager")
        if not manager:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "WebSocket support unavailable")
            return
        connection = WebSocketConnection(self, manager)
        connection.serve()

    def _handle_api_post(self) -> None:
        if self.path == "/api/login":
            self._handle_login()
            return
        if not self._require_auth():
            return
        if self.path == "/api/prompts":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                self._write_json({"error": "invalid json"}, status=400)
                return
            text = payload.get("prompt") or ""
            if not text.strip():
                self._write_json({"error": "prompt is required"}, status=400)
                return
            project_id = payload.get("project_id") or payload.get("project")
            record = APP_CONTEXT["store"].add_prompt(text.strip(), project_id=project_id)
            APP_CONTEXT["audit_logger"].info("Queued prompt %s", record.prompt_id)
            schedule_display_refresh("queued")
            events: Optional[EventStreamer] = APP_CONTEXT.get("events")
            if events:
                events.broadcast_queue()
                events.broadcast_prompt(record.prompt_id)
                events.broadcast_health()
            self._write_json({"prompt_id": record.prompt_id, "status": record.status}, status=201)
        elif self.path == "/api/human_tasks":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                self._write_json({"error": "invalid json"}, status=400)
                return
            title = (payload.get("title") or "").strip()
            description = (payload.get("description") or "").strip()
            project_id = payload.get("project_id") or payload.get("project")
            prompt_id = payload.get("prompt_id") or payload.get("prompt")
            blocking = bool(payload.get("blocking"))
            status = payload.get("status")
            store: HumanTaskStore = APP_CONTEXT["human_tasks"]
            try:
                record = store.create_task(
                    title,
                    description,
                    project_id=project_id,
                    prompt_id=prompt_id,
                    blocking=blocking,
                    status=status,
                )
            except ValueError as exc:
                self._write_json({"error": str(exc)}, status=400)
                return
            registry = APP_CONTEXT.get("projects")
            APP_CONTEXT["audit_logger"].info("Created human task %s", record.task_id)
            schedule_display_refresh("human-task-create")
            events = APP_CONTEXT.get("events")
            if events:
                events.broadcast_health()
            self._write_json({"task": build_human_task_payload(record, registry)}, status=201)
        elif self.path.startswith("/api/prompts/") and self.path.endswith("/retry"):
            parts = self.path.rstrip("/").split("/")
            if len(parts) < 4:
                self._write_json({"error": "invalid retry path"}, status=400)
                return
            prompt_id = parts[-2]
            try:
                record = APP_CONTEXT["store"].retry_prompt(prompt_id)
                APP_CONTEXT["audit_logger"].info("Manual retry requested for %s", prompt_id)
                schedule_display_refresh("retry")
                events = APP_CONTEXT.get("events")
                if events:
                    events.broadcast_queue()
                    events.broadcast_prompt(prompt_id)
                    events.broadcast_health()
                self._write_json({"prompt_id": record.prompt_id, "status": record.status}, status=202)
            except KeyError:
                self._write_json({"error": "prompt not found"}, status=404)
            except ValueError as exc:
                self._write_json({"error": str(exc)}, status=400)
        elif self.path.startswith("/api/prompts/") and self.path.endswith("/cancel"):
            parts = self.path.rstrip("/").split("/")
            if len(parts) < 4:
                self._write_json({"error": "invalid cancel path"}, status=400)
                return
            prompt_id = parts[-2]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                self._write_json({"error": "invalid json"}, status=400)
                return
            restart_requested = bool(payload.get("restart"))
            store: PromptStore = APP_CONTEXT["store"]
            record = store.get_prompt(prompt_id)
            if not record:
                self._write_json({"error": "prompt not found"}, status=404)
                return
            if record.status != "running":
                self._write_json({"error": "prompt is not running"}, status=400)
                return
            worker: Optional[PromptWorker] = APP_CONTEXT.get("worker")
            if not worker:
                self._write_json({"error": "worker unavailable"}, status=503)
                return
            if not worker.request_cancel(prompt_id, restart=restart_requested):
                self._write_json({"error": "prompt is no longer running"}, status=409)
                return
            APP_CONTEXT["audit_logger"].info(
                "Cancellation requested for %s (restart=%s)", prompt_id, restart_requested
            )
            self._write_json(
                {"prompt_id": prompt_id, "status": "canceling", "restart": restart_requested},
                status=202,
            )
        elif self.path == "/api/user/password":
            self._handle_password_change()
        else:
            self._write_json({"error": "unknown endpoint"}, status=404)

    def _handle_api_put(self) -> None:
        if not self._require_auth():
            return
        clean_path = self.path.split("?", 1)[0].rstrip("/")
        if clean_path.startswith("/api/human_tasks/"):
            task_id = clean_path.rsplit("/", 1)[-1]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                self._write_json({"error": "invalid json"}, status=400)
                return
            updates: Dict[str, Any] = {}
            if "title" in payload:
                updates["title"] = payload.get("title") or payload.get("name")
            if "description" in payload:
                updates["description"] = payload.get("description")
            if "status" in payload:
                updates["status"] = payload.get("status")
            if "blocking" in payload:
                updates["blocking"] = bool(payload.get("blocking"))
            if "project_id" in payload or "project" in payload:
                updates["project_id"] = payload.get("project_id") or payload.get("project")
            if "prompt_id" in payload or "prompt" in payload:
                updates["prompt_id"] = payload.get("prompt_id") or payload.get("prompt")
            if not updates:
                self._write_json({"error": "no valid fields provided"}, status=400)
                return
            store: HumanTaskStore = APP_CONTEXT["human_tasks"]
            try:
                record = store.update_task(task_id, **updates)
            except KeyError:
                self._write_json({"error": "human task not found"}, status=404)
                return
            except ValueError as exc:
                self._write_json({"error": str(exc)}, status=400)
                return
            registry = APP_CONTEXT.get("projects")
            APP_CONTEXT["audit_logger"].info("Updated human task %s", task_id)
            schedule_display_refresh("human-task-update")
            events = APP_CONTEXT.get("events")
            if events:
                events.broadcast_health()
            self._write_json({"task": build_human_task_payload(record, registry)})
            return
        if not clean_path.startswith("/api/prompts/"):
            self._write_json({"error": "unknown endpoint"}, status=404)
            return
        prompt_id = clean_path.split("/")[-1]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._write_json({"error": "invalid json"}, status=400)
            return
        new_text = (payload.get("prompt") or payload.get("text") or "").strip()
        if not new_text:
            self._write_json({"error": "prompt text is required"}, status=400)
            return
        try:
            record = APP_CONTEXT["store"].update_prompt_text(prompt_id, new_text)
        except KeyError:
            self._write_json({"error": "prompt not found"}, status=404)
            return
        except ValueError as exc:
            self._write_json({"error": str(exc)}, status=400)
            return
        APP_CONTEXT["audit_logger"].info("Updated prompt %s text", prompt_id)
        events = APP_CONTEXT.get("events")
        if events:
            events.broadcast_queue()
            events.broadcast_prompt(prompt_id)
        self._write_json({"prompt_id": prompt_id, "status": record.status, "text": record.text})

    def _handle_theme_preference_update(self) -> None:
        preferences: Optional[PreferenceStore] = APP_CONTEXT.get("preferences")
        if not preferences:
            self._write_json({"error": "preferences unavailable"}, status=503)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._write_json({"error": "invalid json"}, status=400)
            return
        mode = (payload.get("mode") or payload.get("theme") or "").strip().lower()
        if mode not in {"light", "dark"}:
            self._write_json({"error": "mode must be 'light' or 'dark'"}, status=400)
            return
        try:
            preferences.set_theme_mode(mode)
        except ValueError as exc:
            self._write_json({"error": str(exc)}, status=400)
            return
        except Exception as exc:  # pragma: no cover - unexpected persistence issues
            APP_CONTEXT["audit_logger"].error("Unable to save theme preference: %s", exc)
            self._write_json({"error": "unable to persist preference"}, status=500)
            return
        schedule_display_refresh("theme")
        self._write_json({"mode": mode})

    def _handle_api_put(self) -> None:
        clean_path = self.path.split("?", 1)[0]
        if clean_path == "/api/preferences/theme":
            self._handle_theme_preference_update()
            return
        if not self._require_auth():
            return
        if clean_path.startswith("/api/prompts/"):
            prompt_id = clean_path.rstrip("/").rsplit("/", 1)[-1]
            if not prompt_id:
                self._write_json({"error": "prompt_id required"}, status=400)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                self._write_json({"error": "invalid json"}, status=400)
                return
            new_text = (payload.get("prompt") or "").strip()
            if not new_text:
                self._write_json({"error": "prompt text is required"}, status=400)
                return
            store: PromptStore = APP_CONTEXT["store"]
            try:
                record = store.edit_prompt(prompt_id, new_text)
            except KeyError:
                self._write_json({"error": "prompt not found"}, status=404)
                return
            except ValueError as exc:
                self._write_json({"error": str(exc)}, status=400)
                return
            APP_CONTEXT["audit_logger"].info("Prompt %s edited", prompt_id)
            events = APP_CONTEXT.get("events")
            if events:
                events.broadcast_queue()
                events.broadcast_prompt(prompt_id)
            schedule_display_refresh("edit")
            registry: Optional[ProjectRegistry] = APP_CONTEXT.get("projects")
            self._write_json({"prompt": build_prompt_payload(record, registry)})
        else:
            self._write_json({"error": "unknown endpoint"}, status=404)

    def _handle_api_delete(self) -> None:
        if not self._require_auth():
            return
        clean_path = self.path.split("?", 1)[0]
        if clean_path.startswith("/api/prompts/"):
            prompt_id = clean_path.rstrip("/").rsplit("/", 1)[-1]
            if not prompt_id:
                self._write_json({"error": "prompt_id required"}, status=400)
                return
            store: PromptStore = APP_CONTEXT["store"]
            try:
                store.delete_prompt(prompt_id)
            except KeyError:
                self._write_json({"error": "prompt not found"}, status=404)
                return
            except ValueError as exc:
                self._write_json({"error": str(exc)}, status=400)
                return
            APP_CONTEXT["audit_logger"].info("Prompt %s deleted", prompt_id)
            events = APP_CONTEXT.get("events")
            if events:
                events.broadcast_queue()
                events.broadcast_health()
                events.broadcast_prompt_deleted(prompt_id)
            schedule_display_refresh("delete")
            self._write_json({"prompt_id": prompt_id, "deleted": True})
        elif clean_path.startswith("/api/human_tasks/"):
            task_id = clean_path.rstrip("/").rsplit("/", 1)[-1]
            if not task_id:
                self._write_json({"error": "task_id required"}, status=400)
                return
            store: HumanTaskStore = APP_CONTEXT["human_tasks"]
            try:
                store.delete_task(task_id)
            except KeyError:
                self._write_json({"error": "human task not found"}, status=404)
                return
            APP_CONTEXT["audit_logger"].info("Deleted human task %s", task_id)
            events = APP_CONTEXT.get("events")
            if events:
                events.broadcast_health()
            schedule_display_refresh("human-task-delete")
            self._write_json({"task_id": task_id, "deleted": True})
        else:
            self._write_json({"error": "unknown endpoint"}, status=404)

    def _handle_login(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._write_json({"error": "invalid json"}, status=400)
            return
        email = (payload.get("email") or "").strip()
        password = payload.get("password") or ""
        if not email or not password:
            self._write_json({"error": "email and password are required"}, status=400)
            return
        auth_manager: AuthManager = APP_CONTEXT["auth"]
        user = auth_manager.authenticate(email, password)
        if not user:
            self._write_json({"error": "invalid credentials"}, status=401)
            return
        token = auth_manager.issue_token(user["email"])
        self._write_json({"token": token, "user": auth_manager.user_payload(user)})

    def _handle_password_change(self) -> None:
        if not self.current_user:
            self._write_json({"error": "authorization required"}, status=401)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._write_json({"error": "invalid json"}, status=400)
            return
        current_password = payload.get("current_password") or ""
        new_password = payload.get("new_password") or ""
        if not current_password or not new_password:
            self._write_json({"error": "current and new passwords are required"}, status=400)
            return
        auth_manager: AuthManager = APP_CONTEXT["auth"]
        try:
            auth_manager.change_password(self.current_user.email, current_password, new_password)
        except ValueError as exc:
            self._write_json({"error": str(exc)}, status=400)
            return
        APP_CONTEXT["audit_logger"].info("Password updated for %s", self.current_user.email)
        self._write_json({"status": "ok"})

    def _write_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self._set_common_headers(status=status)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _require_auth(self) -> bool:
        auth_manager: Optional[AuthManager] = APP_CONTEXT.get("auth")
        if not auth_manager:
            return True
        auth_header = self.headers.get("Authorization") or ""
        if not auth_header.startswith("Bearer "):
            self._write_json({"error": "authorization required"}, status=401)
            return False
        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            self._write_json({"error": "authorization required"}, status=401)
            return False
        user = auth_manager.verify_token(token)
        if not user:
            self._write_json({"error": "invalid or expired token"}, status=401)
            return False
        self.current_user = user
        return True


def configure_logging() -> logging.Logger:
    GENERAL_LOG_PATH.touch(exist_ok=True)
    logger = logging.getLogger("agent_backend")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(GENERAL_LOG_PATH)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def start_display_manager(
    store: PromptStore,
    logger: logging.Logger,
    preferences: PreferenceStore | None,
):
    if not _env_flag("ENABLE_EINK_DISPLAY"):
        return None
    try:
        from eink.manager import TaskQueueDisplayManager
    except ImportError as exc:
        logger.warning("E-ink display support unavailable: %s", exc)
        return None

    def _env_int(name: str, default: int) -> int:
        return int(os.environ.get(name, default))

    gpio_chip_raw = os.environ.get("EINK_GPIO_CHIP", "0")
    try:
        gpio_chip: int | str = int(gpio_chip_raw)
    except ValueError:
        gpio_chip = gpio_chip_raw

    config = IT8591Config(
        width=_env_int("EINK_WIDTH", 1872),
        height=_env_int("EINK_HEIGHT", 1404),
        spi_device=_env_int("EINK_SPI_DEVICE", 0),
        spi_channel=_env_int("EINK_SPI_CHANNEL", 0),
        spi_hz=_env_int("EINK_SPI_HZ", 24_000_000),
        gpio_chip=gpio_chip,
        rst_pin=_env_int("EINK_RST_PIN", 17),
        busy_pin=_env_int("EINK_BUSY_PIN", 24),
        cs_pin=_env_int("EINK_CS_PIN", 8),
        vcom_mv=_env_int("EINK_VCOM_MV", 1800),
        rotate=_env_int("EINK_ROTATE", IT8951_ROTATE_180),
    )
    manager = TaskQueueDisplayManager(store, logger, enabled=True, config=config, preferences=preferences)
    manager.start()
    return manager


def main(host: str = "0.0.0.0", port: int = 8080) -> None:
    ensure_dirs()
    preferred_project = os.environ.get("DEFAULT_PROJECT_ID")
    project_registry = ProjectRegistry(PROJECTS_DIR, preferred_project)
    audit_logger = configure_logging()
    store = PromptStore(PROMPT_DB_PATH, project_registry)
    human_task_store = HumanTaskStore(HUMAN_TASK_DB_PATH, project_registry)
    preference_store = PreferenceStore(PREFERENCES_PATH)
    auth_manager = AuthManager(DATA_DIR)
    auth_manager.ensure_user("ulfurk@ulfurk.com", "dehost#1")
    ssh_key_manager = SSHKeyManager(DATA_DIR, audit_logger)
    try:
        ssh_key_manager.ensure_default_keys()
    except SSHKeyError as exc:
        audit_logger.error("SSH key initialization failed: %s", exc)
    ws_manager = WebSocketManager(auth_manager, audit_logger)
    events = EventStreamer(store, audit_logger, ws_manager, project_registry, human_task_store)
    ws_manager.event_streamer = events
    recovered_prompt_ids = store.consume_recovered_prompts()
    if recovered_prompt_ids:
        events.broadcast_queue()
        for prompt_id in recovered_prompt_ids:
            events.broadcast_prompt(prompt_id)
        events.broadcast_health()
    runner = CodexRunner(REPO_ROOT, events, audit_logger)
    display_manager = start_display_manager(store, audit_logger, preference_store)
    worker = PromptWorker(store, runner, audit_logger, display_manager, event_streamer=events)
    worker.start()
    health_thread = HealthBroadcaster(events)
    health_thread.start()

    global APP_CONTEXT  # pylint: disable=global-statement
    APP_CONTEXT = {
        "store": store,
        "human_tasks": human_task_store,
        "audit_logger": audit_logger,
        "display_manager": display_manager,
        "auth": auth_manager,
        "ws_manager": ws_manager,
        "events": events,
        "worker": worker,
        "ssh_keys": ssh_key_manager,
        "projects": project_registry,
        "preferences": preference_store,
    }
    if recovered_prompt_ids:
        schedule_display_refresh("recovered prompts")

    server = ThreadingHTTPServer((host, port), AgentHTTPRequestHandler)
    audit_logger.info("Agent backend listening on %s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        audit_logger.info("Shutting down...")
        worker.stop()
        if display_manager:
            display_manager.stop()
            display_manager.join(timeout=5)
        server.server_close()
        health_thread.stop()
        health_thread.join(timeout=5)


if __name__ == "__main__":
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_PORT", "8080"))
    main(host, port)
