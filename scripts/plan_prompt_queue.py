#!/usr/bin/env python3
"""Queue upgrade prompts sequentially based on the structured plan file."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN_PATH = REPO_ROOT / "docs" / "upgrade_plan.json"
DEFAULT_PROMPT_DB = REPO_ROOT / "data" / "prompts.json"
DEFAULT_LOG_DIR = REPO_ROOT / "logs"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlanTask:
    """Wrapper around the plan's task dict so we can track status cleanly."""

    def __init__(self, data: Dict[str, Any]):
        self.data = data

    @property
    def task_id(self) -> str:
        return str(self.data.get("id") or "")

    @property
    def title(self) -> str:
        return str(self.data.get("title") or "Untitled task").strip() or "Untitled task"

    @property
    def prompt(self) -> str:
        return str(self.data.get("prompt") or "")

    @property
    def project_id(self) -> Optional[str]:
        project = self.data.get("project_id")
        return str(project) if project else None

    @property
    def status(self) -> str:
        return str(self.data.get("status") or "pending").lower()

    def mark_queued(self, prompt_id: str, timestamp: str) -> None:
        self.data["status"] = "queued"
        self.data["last_prompt_id"] = prompt_id
        self.data["last_queued_at"] = timestamp
        self.data["queued_count"] = int(self.data.get("queued_count") or 0) + 1


class UpgradePlan:
    """Loads and persists the structured upgrade plan."""

    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()
        self.tasks: List[PlanTask] = [PlanTask(entry) for entry in self.data.get("tasks", [])]

    def _load(self) -> Dict[str, Any]:
        raw = self.path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {self.path}: {exc}") from exc
        if "tasks" not in data or not isinstance(data["tasks"], list):
            raise ValueError(f"{self.path} is missing a 'tasks' list")
        return data

    def pending_tasks(self) -> List[PlanTask]:
        return [task for task in self.tasks if task.status == "pending"]

    def save(self) -> None:
        payload = {"tasks": [task.data for task in self.tasks]}
        for key, value in self.data.items():
            if key != "tasks":
                payload[key] = value
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


class PromptQueue:
    """Minimal prompt queue helper that mirrors backend PromptStore serialization."""

    def __init__(self, db_path: Path, logs_dir: Path):
        self.db_path = db_path
        self.logs_dir = logs_dir
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.records: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.db_path.exists():
            self.db_path.write_text("{}\n", encoding="utf-8")
            return {}
        raw = self.db_path.read_text(encoding="utf-8")
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): value for key, value in data.items()}

    def _persist(self) -> None:
        self.db_path.write_text(json.dumps(self.records, indent=2) + "\n", encoding="utf-8")

    def add_prompt(self, text: str, project_id: Optional[str]) -> Dict[str, Any]:
        prompt_id = uuid.uuid4().hex
        timestamp = utcnow_iso()
        log_path = str(self.logs_dir / f"prompt_{prompt_id}.log")
        record = {
            "prompt_id": prompt_id,
            "text": text,
            "status": "queued",
            "created_at": timestamp,
            "updated_at": timestamp,
            "log_path": log_path,
            "result_summary": None,
            "attempts": 0,
            "project_id": project_id,
        }
        self.records[prompt_id] = record
        self._persist()
        return record


@dataclass
class QueueResult:
    task_id: str
    prompt_id: str
    title: str
    queued_at: str


def queue_plan_tasks(
    plan: UpgradePlan,
    queue: PromptQueue,
    *,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> List[QueueResult]:
    pending = plan.pending_tasks()
    if limit is not None:
        pending = pending[:limit]
    results: List[QueueResult] = []
    for task in pending:
        prompt_text = task.prompt.strip()
        if not prompt_text:
            continue
        if dry_run:
            results.append(QueueResult(task.task_id, "dry-run", task.title, utcnow_iso()))
            continue
        record = queue.add_prompt(prompt_text, task.project_id)
        task.mark_queued(record["prompt_id"], record["created_at"])
        results.append(QueueResult(task.task_id, record["prompt_id"], task.title, record["created_at"]))
    if results and not dry_run:
        plan.save()
    return results


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Queue prompts defined in docs/upgrade_plan.json sequentially.")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN_PATH, help="Path to the structured plan JSON file.")
    parser.add_argument(
        "--prompts",
        type=Path,
        default=DEFAULT_PROMPT_DB,
        help="Path to prompts.json (default: %(default)s).",
    )
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory where prompt log files live (default: %(default)s).",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="Number of prompts to queue (default: all pending).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be queued without mutating prompts.json or the plan.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    try:
        plan = UpgradePlan(args.plan)
    except (OSError, ValueError) as exc:
        print(f"error: unable to load plan: {exc}", file=sys.stderr)
        return 1
    queue = PromptQueue(args.prompts, args.logs_dir)
    initial_pending = plan.pending_tasks()
    limit = args.count if args.count is not None else None
    results = queue_plan_tasks(plan, queue, limit=limit, dry_run=args.dry_run)
    if not results:
        print("No prompts were queued; nothing to do.")
        return 0
    total = len(results)
    print(f"Queued {total} prompt{'s' if total != 1 else ''}.")
    for idx, result in enumerate(results, start=1):
        print(f"[{idx}/{total}] {result.title} -> prompt {result.prompt_id} at {result.queued_at}")
        next_index = idx
        if next_index < len(initial_pending):
            next_task = initial_pending[next_index]
            print(f"    Reminder: queue next step '{next_task.title}' when ready.")
    remaining = len(plan.pending_tasks())
    if remaining:
        print(f"{remaining} plan task(s) are still pending.")
    else:
        print("All plan tasks have been queued. Stop here as requested.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
