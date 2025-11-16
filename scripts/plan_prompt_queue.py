#!/usr/bin/env python3
"""Queue upgrade prompts sequentially based on the structured plan file."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib import error, request


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
DEFAULT_PLAN_PATH = REPO_ROOT / "docs" / "upgrade_plan.json"
DEFAULT_PROMPT_DB = DATA_DIR / "prompts.json"
DEFAULT_LOG_DIR = REPO_ROOT / "logs"
PROGRESS_LOG_PATH = DEFAULT_LOG_DIR / "progress.log"
DEFAULT_API_URL = os.environ.get("AGENT_API_URL")
DEFAULT_API_HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
DEFAULT_API_PORT = int(os.environ.get("AGENT_PORT", "8080"))
DEFAULT_API_TIMEOUT = float(os.environ.get("AGENT_API_TIMEOUT", "10"))
DEFAULT_API_EMAIL = os.environ.get("AGENT_EMAIL")
DEFAULT_API_PASSWORD = os.environ.get("AGENT_PASSWORD")
DEFAULT_API_TOKEN = os.environ.get("AGENT_TOKEN")
AUTH_TOKEN_TTL = int(os.environ.get("AUTH_TOKEN_TTL", "43200"))


class PromptQueueError(Exception):
    """Raised when queue operations fail."""


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


class FilePromptQueue:
    """Queue helper that writes directly to prompts.json when the backend is stopped."""

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
            "enqueued_at": timestamp,
            "log_path": log_path,
            "result_summary": None,
            "attempts": 0,
            "project_id": project_id,
            "started_at": None,
            "current_wait_seconds": None,
            "last_wait_seconds": None,
            "last_run_seconds": None,
            "last_finished_at": None,
            "human_task_id": None,
            "reply_to_prompt_id": None,
            "server_restart_required": False,
            "server_restart_marked_at": None,
        }
        self.records[prompt_id] = record
        self._persist()
        return {
            "prompt_id": prompt_id,
            "status": record["status"],
            "queued_at": timestamp,
        }


PromptQueue = FilePromptQueue


class APIPromptQueue:
    """Queues prompts via the live HTTP API and verifies persistence."""

    def __init__(
        self,
        base_url: str,
        data_dir: Path,
        prompts_path: Path,
        log_path: Path,
        *,
        timeout: float = DEFAULT_API_TIMEOUT,
        email: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        use_login: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.data_dir = data_dir
        self.prompts_path = prompts_path
        self.log_path = log_path
        self.timeout = timeout
        self.email = email
        self.password = password
        self._token = token
        self.use_login = use_login

    def add_prompt(self, text: str, project_id: Optional[str]) -> Dict[str, Any]:
        token = self._ensure_token()
        payload: Dict[str, Any] = {"prompt": text}
        if project_id:
            payload["project_id"] = project_id
        response = _post_json(f"{self.base_url}/api/prompts", payload, token, self.timeout)
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            raise PromptQueueError("API response missing prompt_id")
        detail = self._fetch_prompt_detail(prompt_id)
        queued_at = (
            detail.get("enqueued_at")
            or detail.get("created_at")
            or detail.get("updated_at")
            or utcnow_iso()
        )
        status = detail.get("status") or response.get("status") or "queued"
        self._confirm_persisted(prompt_id)
        return {"prompt_id": prompt_id, "queued_at": queued_at, "status": status}

    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        if self.use_login:
            if not self.email or not self.password:
                raise PromptQueueError("Email and password are required when --login is set")
            payload = {"email": self.email, "password": self.password}
            response = _post_json(f"{self.base_url}/api/login", payload, None, self.timeout)
            token = response.get("token")
            if not token:
                raise PromptQueueError("Login succeeded but no token was returned")
            self._token = token
            return token
        token = issue_service_token(self.data_dir, self.email)
        self._token = token
        return token

    def _fetch_prompt_detail(self, prompt_id: str) -> Dict[str, Any]:
        token = self._ensure_token()
        path = f"{self.base_url}/api/prompts/{prompt_id}"
        detail = _get_json(path, token, self.timeout)
        if not isinstance(detail, dict):
            raise PromptQueueError(f"Prompt {prompt_id} lookup did not return a JSON object")
        return detail

    def _confirm_persisted(self, prompt_id: str) -> None:
        file_ok = False
        log_ok = False
        for _ in range(5):
            file_ok = prompt_exists_in_db(self.prompts_path, prompt_id)
            log_ok = prompt_logged(self.log_path, prompt_id)
            if file_ok and log_ok:
                return
            time.sleep(0.2)
        missing: list[str] = []
        if not file_ok:
            missing.append(str(self.prompts_path))
        if not log_ok:
            missing.append(str(self.log_path))
        raise PromptQueueError(
            f"Prompt {prompt_id} not reflected in {' and '.join(missing)} after queueing"
        )


@dataclass
class QueueResult:
    task_id: str
    prompt_id: str
    title: str
    queued_at: str


def _urlsafe_b64encode(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def issue_service_token(data_dir: Path, email: Optional[str]) -> str:
    target_email = (email or discover_default_email(data_dir) or "").strip()
    if not target_email:
        raise PromptQueueError("No users found; unable to issue a service token")
    users = load_users(data_dir)
    if target_email.lower() not in users:
        raise PromptQueueError(f"User {target_email} does not exist in data/users.json")
    secret_path = data_dir / ".auth_secret"
    try:
        secret = secret_path.read_bytes()
    except OSError as exc:
        raise PromptQueueError(f"Unable to read {secret_path}: {exc}") from exc
    issued_at = int(time.time())
    payload = {"sub": users[target_email.lower()], "iat": issued_at, "exp": issued_at + AUTH_TOKEN_TTL}
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _urlsafe_b64encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = b".".join([header_b64, payload_b64])
    signature = _urlsafe_b64encode(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return b".".join([signing_input, signature]).decode("ascii")


def discover_default_email(data_dir: Path) -> Optional[str]:
    users = load_users(data_dir)
    if not users:
        return None
    return next(iter(users.values()))


def load_users(data_dir: Path) -> Dict[str, str]:
    users_path = data_dir / "users.json"
    try:
        contents = users_path.read_text(encoding="utf-8")
        data = json.loads(contents or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    result: Dict[str, str] = {}
    for entry in data.get("users", []):
        email = str(entry.get("email") or "").strip()
        if email:
            result[email.lower()] = email
    return result


def _post_json(url: str, payload: Dict[str, Any], token: Optional[str], timeout: float) -> Dict[str, Any]:
    encoded = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "plan-queue/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return json.loads(body)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8")
        try:
            message = json.loads(detail).get("error") or detail
        except json.JSONDecodeError:
            message = detail or exc.reason
        raise PromptQueueError(f"HTTP {exc.code}: {message}") from None
    except error.URLError as exc:
        raise PromptQueueError(f"Unable to reach {url}: {exc.reason}") from None


def _get_json(url: str, token: str, timeout: float) -> Dict[str, Any]:
    req = request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "plan-queue/1.0")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return json.loads(body)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8")
        try:
            message = json.loads(detail).get("error") or detail
        except json.JSONDecodeError:
            message = detail or exc.reason
        raise PromptQueueError(f"HTTP {exc.code}: {message}") from None
    except error.URLError as exc:
        raise PromptQueueError(f"Unable to reach {url}: {exc.reason}") from None


def prompt_exists_in_db(db_path: Path, prompt_id: str) -> bool:
    try:
        data = json.loads(db_path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return False
    entry = data.get(prompt_id)
    return isinstance(entry, dict)


def prompt_logged(log_path: Path, prompt_id: str) -> bool:
    if not log_path.exists():
        return False
    needle = f"Queued prompt {prompt_id}"
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            tail = deque(handle, maxlen=400)
    except OSError:
        return False
    return any(needle in line for line in tail)


def build_api_base_url(api_url: Optional[str], host: str, port: int) -> str:
    if api_url:
        return api_url.rstrip("/")
    normalized_host = host or "127.0.0.1"
    if normalized_host in {"0.0.0.0", "::"}:
        normalized_host = "127.0.0.1"
    return f"http://{normalized_host}:{port}".rstrip("/")


def queue_plan_tasks(
    plan: UpgradePlan,
    queue: FilePromptQueue | APIPromptQueue,
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
        task.mark_queued(record["prompt_id"], record["queued_at"])
        results.append(QueueResult(task.task_id, record["prompt_id"], task.title, record["queued_at"]))
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
        help="Path to prompts.json (used for verification or direct writes; default: %(default)s).",
    )
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory where prompt log files live (used for verification; default: %(default)s).",
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
    parser.add_argument(
        "--direct-write",
        action="store_true",
        help="Write prompts.json directly instead of calling the HTTP API (only when the backend is stopped).",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help="Override the API base URL (default: derived from --api-host/--api-port).",
    )
    parser.add_argument(
        "--api-host",
        default=DEFAULT_API_HOST,
        help=f"Backend host when --api-url is not set (default: %(default)s).",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=DEFAULT_API_PORT,
        help="Backend port when --api-url is not set (default: %(default)s).",
    )
    parser.add_argument(
        "--api-timeout",
        type=float,
        default=DEFAULT_API_TIMEOUT,
        help="HTTP timeout in seconds for API calls (default: %(default)s).",
    )
    parser.add_argument(
        "--api-email",
        default=DEFAULT_API_EMAIL,
        help="Email used when issuing service tokens or logging in (default: first configured user).",
    )
    parser.add_argument(
        "--api-password",
        default=DEFAULT_API_PASSWORD,
        help="Password to use when --login is specified.",
    )
    parser.add_argument(
        "--api-token",
        default=DEFAULT_API_TOKEN,
        help="Existing bearer token (skips login/service token).",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Authenticate via email/password instead of issuing a local service token.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    try:
        plan = UpgradePlan(args.plan)
    except (OSError, ValueError) as exc:
        print(f"error: unable to load plan: {exc}", file=sys.stderr)
        return 1
    if args.direct_write:
        queue: FilePromptQueue | APIPromptQueue = FilePromptQueue(args.prompts, args.logs_dir)
    else:
        base_url = build_api_base_url(args.api_url, args.api_host, args.api_port)
        progress_log = args.logs_dir / "progress.log"
        queue = APIPromptQueue(
            base_url=base_url,
            data_dir=DATA_DIR,
            prompts_path=args.prompts,
            log_path=progress_log,
            timeout=args.api_timeout,
            email=args.api_email,
            password=args.api_password,
            token=args.api_token,
            use_login=args.login,
        )
    initial_pending = plan.pending_tasks()
    limit = args.count if args.count is not None else None
    try:
        results = queue_plan_tasks(plan, queue, limit=limit, dry_run=args.dry_run)
    except PromptQueueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
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
