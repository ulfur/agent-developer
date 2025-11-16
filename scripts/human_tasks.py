#!/usr/bin/env python3
"""CLI helper for managing the Human Tasks queue."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any, Dict, Iterable, List
from urllib import error, request


DEFAULT_HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("AGENT_PORT", "8080"))
DEFAULT_API_URL = os.environ.get("AGENT_API_URL")
DEFAULT_PROJECT = os.environ.get("DEFAULT_PROJECT_ID")
DEFAULT_EMAIL = os.environ.get("AGENT_EMAIL")
DEFAULT_PASSWORD = os.environ.get("AGENT_PASSWORD")
DEFAULT_TOKEN = os.environ.get("AGENT_TOKEN")
DEFAULT_TIMEOUT = float(os.environ.get("AGENT_API_TIMEOUT", "10"))
VALID_STATUSES = ("open", "in_progress", "resolved")


class CLIError(Exception):
    """Raised when the helper encounters an unrecoverable error."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect and update the Nightshift Human Tasks queue.",
        epilog=(
            "Descriptions default to stdin when --description is omitted. "
            "Set AGENT_EMAIL/AGENT_PASSWORD or pass --email/--password for authentication."
        ),
    )
    parser.add_argument("--url", dest="base_url", default=DEFAULT_API_URL, help="Override API base URL.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Backend host when --url is not set.")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="Backend port when --url is not set.")
    parser.add_argument("-e", "--email", default=DEFAULT_EMAIL, help="Login email.")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Login password.")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Existing bearer token.")
    parser.add_argument("--timeout", default=DEFAULT_TIMEOUT, type=float, help="HTTP timeout in seconds.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List human tasks.")
    list_parser.add_argument("--status", action="append", default=[], help="Filter by status (repeatable).")
    list_parser.add_argument(
        "--blocking-only",
        action="store_true",
        help="Only show tasks flagged as blocking.",
    )
    list_parser.add_argument("--limit", type=int, default=0, help="Limit the number of rows printed.")
    list_parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of pretty output.")

    add_parser = subparsers.add_parser("add", help="Create a new human task.")
    add_parser.add_argument("title", help="Short title for the task.")
    add_parser.add_argument(
        "-d",
        "--description",
        help="Longer description (defaults to stdin when omitted).",
    )
    add_parser.add_argument("--project", default=DEFAULT_PROJECT, help="Associate with this project id.")
    add_parser.add_argument("--prompt", help="Related prompt id.")
    add_parser.add_argument(
        "--status",
        choices=VALID_STATUSES,
        default="open",
        help="Initial status (default: %(default)s).",
    )
    add_parser.add_argument(
        "--blocking",
        action="store_true",
        help="Mark the task as blocking automation.",
    )

    update_parser = subparsers.add_parser("update", help="Edit an existing task.")
    update_parser.add_argument("task_id", help="Task id to edit.")
    update_parser.add_argument("--title", help="New title.")
    update_parser.add_argument("--description", help="New description.")
    update_parser.add_argument("--project", help="New project id.")
    update_parser.add_argument("--prompt", help="New prompt id.")
    update_parser.add_argument("--status", choices=VALID_STATUSES, help="New status value.")
    update_parser.add_argument(
        "--blocking",
        dest="blocking",
        action="store_true",
        help="Mark the task as blocking.",
    )
    update_parser.add_argument(
        "--unblock",
        dest="blocking",
        action="store_false",
        help="Clear the blocking flag.",
    )
    update_parser.set_defaults(blocking=None)

    delete_parser = subparsers.add_parser("delete", help="Remove a task that was logged in error.")
    delete_parser.add_argument("task_id", help="Task id to delete.")

    resolve_parser = subparsers.add_parser("resolve", help="Mark a task as resolved and non-blocking.")
    resolve_parser.add_argument("task_id", help="Task id to resolve.")

    clear_parser = subparsers.add_parser(
        "clear",
        help="Delete all human tasks or limit to specific statuses.",
    )
    clear_parser.add_argument(
        "--status",
        dest="statuses",
        action="append",
        choices=VALID_STATUSES,
        help="Only remove tasks with this status. Repeat for multiple statuses.",
    )

    return parser.parse_args()


def build_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        return args.base_url.rstrip("/")
    return f"http://{args.host}:{args.port}".rstrip("/")


def request_api(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: Dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float,
) -> Dict[str, Any]:
    url = f"{base_url}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return json.loads(body)
    except error.HTTPError as exc:  # pragma: no cover - CLI surface
        detail = exc.read().decode("utf-8")
        try:
            message = json.loads(detail).get("error") or detail
        except json.JSONDecodeError:
            message = detail or exc.reason
        raise CLIError(f"API request failed ({exc.code}): {message}") from None
    except error.URLError as exc:  # pragma: no cover - CLI surface
        raise CLIError(f"Unable to reach {url}: {exc.reason}") from None


def login(base_url: str, email: str | None, password: str | None, timeout: float) -> str:
    if not email:
        email = input("Email: ").strip()
    if not email:
        raise CLIError("Email is required.")
    if not password:
        password = getpass.getpass("Password: ")
    payload = {"email": email, "password": password}
    response = request_api(base_url, "/api/login", method="POST", payload=payload, token=None, timeout=timeout)
    token = response.get("token")
    if not token:
        raise CLIError("Login succeeded but no token was returned.")
    return token


def read_description(value: str | None) -> str:
    if value:
        return value.strip()
    if not sys.stdin.isatty():
        content = sys.stdin.read().strip()
        if content:
            return content
    return ""


def render_task(task: Dict[str, Any]) -> str:
    status = (task.get("status") or "").replace("_", " ").title() or "Unknown"
    blocking = "blocking" if task.get("blocking") else ""
    project = (
        task.get("project", {}).get("name")
        or task.get("project_id")
        or (task.get("project", {}).get("id") if isinstance(task.get("project"), dict) else "")
    ) or "unscoped"
    prompt = task.get("prompt_id")
    title = task.get("title") or ""
    description = (task.get("description") or "").strip()
    updated = task.get("updated_at") or "unknown"
    segments = [
        f"{task.get('task_id')} · {status}",
        f"project: {project}",
    ]
    if blocking:
        segments.append(blocking)
    if prompt:
        segments.append(f"prompt {prompt}")
    header = " | ".join(segment for segment in segments if segment)
    body_lines = [header, f"  {title}"]
    if description:
        body_lines.append(f"  {description}")
    body_lines.append(f"  updated {updated}")
    return "\n".join(body_lines)


def filter_tasks(
    tasks: Iterable[Dict[str, Any]],
    *,
    statuses: List[str],
    blocking_only: bool,
) -> List[Dict[str, Any]]:
    normalized_statuses = {status.strip().lower() for status in statuses if status}
    result = []
    for task in tasks:
        status = (task.get("status") or "").lower()
        if normalized_statuses and status not in normalized_statuses:
            continue
        if blocking_only and not task.get("blocking"):
            continue
        result.append(task)
    return result


def handle_list(args: argparse.Namespace, base_url: str, token: str) -> int:
    data = request_api(base_url, "/api/human_tasks", method="GET", token=token, timeout=args.timeout)
    tasks = data.get("tasks") or []
    filtered = filter_tasks(tasks, statuses=args.status, blocking_only=args.blocking_only)
    if args.limit and args.limit > 0:
        filtered = filtered[: args.limit]
    if args.json:
        print(json.dumps(filtered, indent=2))
        return 0
    summary = data.get("summary") or {}
    blocking_count = summary.get("blocking_count", 0)
    status_counts = summary.get("status_counts") or {}
    total = summary.get("total", len(tasks))
    print(f"Human Tasks: {total} total · {blocking_count} blocking")
    if status_counts:
        status_line = ", ".join(f"{status}: {count}" for status, count in status_counts.items())
        print(status_line)
    if not filtered:
        print("No matching tasks.")
        return 0
    print("")
    for entry in filtered:
        print(render_task(entry))
        print("")
    return 0


def handle_add(args: argparse.Namespace, base_url: str, token: str) -> int:
    description = read_description(args.description)
    payload: Dict[str, Any] = {
        "title": args.title,
        "description": description,
        "project_id": args.project,
        "prompt_id": args.prompt,
        "status": args.status,
        "blocking": bool(args.blocking),
    }
    data = request_api(base_url, "/api/human_tasks", method="POST", payload=payload, token=token, timeout=args.timeout)
    task = data.get("task") or {}
    print(f"Created task {task.get('task_id')} ({task.get('status')})")
    return 0


def handle_update(args: argparse.Namespace, base_url: str, token: str) -> int:
    payload: Dict[str, Any] = {}
    if args.title is not None:
        payload["title"] = args.title
    if args.description is not None:
        payload["description"] = args.description
    if args.project is not None:
        payload["project_id"] = args.project
    if args.prompt is not None:
        payload["prompt_id"] = args.prompt
    if args.status is not None:
        payload["status"] = args.status
    if args.blocking is not None:
        payload["blocking"] = bool(args.blocking)
    if not payload:
        raise CLIError("No fields provided to update.")
    data = request_api(
        base_url,
        f"/api/human_tasks/{args.task_id}",
        method="PUT",
        payload=payload,
        token=token,
        timeout=args.timeout,
    )
    task = data.get("task") or {}
    print(f"Updated task {task.get('task_id')} ({task.get('status')})")
    return 0


def handle_delete(args: argparse.Namespace, base_url: str, token: str) -> int:
    request_api(
        base_url,
        f"/api/human_tasks/{args.task_id}",
        method="DELETE",
        payload=None,
        token=token,
        timeout=args.timeout,
    )
    print(f"Deleted task {args.task_id}")
    return 0


def handle_resolve(args: argparse.Namespace, base_url: str, token: str) -> int:
    payload = {"status": "resolved", "blocking": False}
    data = request_api(
        base_url,
        f"/api/human_tasks/{args.task_id}",
        method="PUT",
        payload=payload,
        token=token,
        timeout=args.timeout,
    )
    task = data.get("task") or {}
    print(f"Resolved task {task.get('task_id')}")
    return 0


def handle_clear(args: argparse.Namespace, base_url: str, token: str) -> int:
    payload: Dict[str, Any] | None = None
    if args.statuses:
        payload = {"statuses": args.statuses}
    data = request_api(
        base_url,
        "/api/human_tasks",
        method="DELETE",
        payload=payload,
        token=token,
        timeout=args.timeout,
    )
    cleared = data.get("cleared", 0)
    suffix = "" if cleared == 1 else "s"
    print(f"Cleared {cleared} human task{suffix}.")
    return 0


def main() -> int:
    args = parse_args()
    base_url = build_base_url(args)
    token = args.token
    try:
        if not token:
            token = login(base_url, args.email, args.password, args.timeout)
        if args.command == "list":
            return handle_list(args, base_url, token)
        if args.command == "add":
            return handle_add(args, base_url, token)
        if args.command == "update":
            return handle_update(args, base_url, token)
        if args.command == "delete":
            return handle_delete(args, base_url, token)
        if args.command == "resolve":
            return handle_resolve(args, base_url, token)
        if args.command == "clear":
            return handle_clear(args, base_url, token)
        raise CLIError(f"Unknown command: {args.command}")
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
