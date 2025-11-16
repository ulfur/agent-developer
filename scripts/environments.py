#!/usr/bin/env python3
"""CLI helper for inspecting and managing Nightshift environments."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib import error, request

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from environments import VALID_HEALTH_STATUSES, VALID_LIFECYCLE_STATES  # type: ignore  # pylint: disable=wrong-import-position

DEFAULT_HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("AGENT_PORT", "8080"))
DEFAULT_API_URL = os.environ.get("AGENT_API_URL")
DEFAULT_PROJECT = os.environ.get("DEFAULT_PROJECT_ID")
DEFAULT_EMAIL = os.environ.get("AGENT_EMAIL")
DEFAULT_PASSWORD = os.environ.get("AGENT_PASSWORD")
DEFAULT_TOKEN = os.environ.get("AGENT_TOKEN")
DEFAULT_TIMEOUT = float(os.environ.get("AGENT_API_TIMEOUT", "10"))


class CLIError(Exception):
    """Raised when the helper encounters an unrecoverable error."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage the Nightshift environment registry via /api/environments endpoints.",
        epilog=(
            "Commands authenticate via /api/login when --token is missing. "
            "Descriptions default to stdin when omitted in create mode."
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

    list_parser = subparsers.add_parser("list", help="List all environments.")
    list_parser.add_argument("--project", help="Filter by project id.")
    list_parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of text.")

    show_parser = subparsers.add_parser("show", help="Show the details for one environment.")
    show_parser.add_argument("identifier", help="Environment id or slug.")
    show_parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of text.")

    create_parser = subparsers.add_parser("create", help="Register a new environment.")
    create_parser.add_argument("--project", default=DEFAULT_PROJECT, help="Project id to associate.")
    create_parser.add_argument("--slug", required=True, help="Unique slug (used in URLs and CLI filters).")
    create_parser.add_argument("--name", required=True, help="Display name.")
    create_parser.add_argument("-d", "--description", help="Longer description (defaults to stdin).")
    create_parser.add_argument("--hostname", required=True, help="Primary host or DNS name.")
    create_parser.add_argument("--host-ip", dest="host_ip", help="Host IP address.")
    create_parser.add_argument("--host-provider", dest="host_provider", help="Cloud/provider label.")
    create_parser.add_argument("--host-region", dest="host_region", help="Region/zone tag.")
    create_parser.add_argument("--host-notes", dest="host_notes", help="Host notes.")
    create_parser.add_argument("--owner-name", dest="owner_name", required=True, help="Environment owner name.")
    create_parser.add_argument("--owner-email", dest="owner_email", help="Owner email.")
    create_parser.add_argument("--owner-slack", dest="owner_slack", help="Owner Slack/channel.")
    create_parser.add_argument("--owner-role", dest="owner_role", help="Owner role/title.")
    create_parser.add_argument(
        "--state",
        choices=sorted(VALID_LIFECYCLE_STATES),
        default="active",
        help="Lifecycle state (default: %(default)s).",
    )
    create_parser.add_argument("--lifecycle-notes", dest="lifecycle_notes", help="Lifecycle notes.")
    create_parser.add_argument(
        "--health-status",
        dest="health_status",
        choices=sorted(VALID_HEALTH_STATUSES),
        default="unknown",
        help="Health status (default: %(default)s).",
    )
    create_parser.add_argument("--health-url", dest="health_url", help="Health URL to poll.")
    create_parser.add_argument("--health-notes", dest="health_notes", help="Health notes.")
    create_parser.add_argument("--health-checked-at", dest="health_checked_at", help="ISO timestamp for the last check.")
    create_parser.add_argument(
        "--port",
        action="append",
        dest="ports",
        default=[],
        help="Define a port as name:port[:protocol[:url[:description]]] (repeatable).",
    )
    create_parser.add_argument(
        "--tag",
        action="append",
        dest="tags",
        default=[],
        help="Attach metadata as key=value (repeatable).",
    )

    update_parser = subparsers.add_parser("update", help="Edit an existing environment.")
    update_parser.add_argument("identifier", help="Environment id or slug.")
    update_parser.add_argument("--project", help="New project id.")
    update_parser.add_argument("--slug", help="New slug.")
    update_parser.add_argument("--name", help="New display name.")
    update_parser.add_argument("-d", "--description", help="Replace the description.")
    update_parser.add_argument("--hostname", help="New hostname.")
    update_parser.add_argument("--host-ip", dest="host_ip", help="Host IP address.")
    update_parser.add_argument("--host-provider", dest="host_provider", help="Cloud/provider label.")
    update_parser.add_argument("--host-region", dest="host_region", help="Region/zone tag.")
    update_parser.add_argument("--host-notes", dest="host_notes", help="Host notes.")
    update_parser.add_argument("--owner-name", dest="owner_name", help="Owner name.")
    update_parser.add_argument("--owner-email", dest="owner_email", help="Owner email.")
    update_parser.add_argument("--owner-slack", dest="owner_slack", help="Owner Slack/channel.")
    update_parser.add_argument("--owner-role", dest="owner_role", help="Owner role/title.")
    update_parser.add_argument("--state", choices=sorted(VALID_LIFECYCLE_STATES), help="Lifecycle state.")
    update_parser.add_argument("--lifecycle-notes", dest="lifecycle_notes", help="Lifecycle notes.")
    update_parser.add_argument("--health-status", dest="health_status", choices=sorted(VALID_HEALTH_STATUSES), help="Health status.")
    update_parser.add_argument("--health-url", dest="health_url", help="Health URL.")
    update_parser.add_argument("--health-notes", dest="health_notes", help="Health notes.")
    update_parser.add_argument("--health-checked-at", dest="health_checked_at", help="ISO timestamp for the last check.")
    update_parser.add_argument(
        "--port",
        action="append",
        dest="ports",
        default=[],
        help="Replace ports using name:port[:protocol[:url[:description]]].",
    )
    update_parser.add_argument(
        "--tag",
        action="append",
        dest="tags",
        default=[],
        help="Replace metadata (key=value).",
    )

    delete_parser = subparsers.add_parser("delete", help="Remove an environment entry.")
    delete_parser.add_argument("identifier", help="Environment id or slug.")

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
    token: str | None,
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


def ensure_token(args: argparse.Namespace, base_url: str) -> str:
    if args.token:
        return args.token
    return login(base_url, args.email, args.password, args.timeout)


def read_description(value: Optional[str]) -> str:
    if value is not None:
        return value.strip()
    if not sys.stdin.isatty():
        content = sys.stdin.read().strip()
        if content:
            return content
    return ""


def parse_ports(entries: Iterable[str]) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for entry in entries or []:
        parts = entry.split(":", 4)
        while len(parts) < 5:
            parts.append("")
        name, port_str, protocol, url, description = parts
        name = name.strip()
        port_str = port_str.strip()
        if not name or not port_str:
            raise CLIError(f"Invalid port definition: {entry}")
        try:
            port = int(port_str)
        except ValueError:
            raise CLIError(f"Invalid port number for '{name}': {port_str}") from None
        payload = {
            "name": name,
            "port": port,
            "protocol": (protocol or "tcp").strip().lower() or "tcp",
            "url": url.strip(),
            "description": description.strip(),
        }
        parsed.append(payload)
    return parsed


def parse_tags(entries: Iterable[str]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    for entry in entries or []:
        if "=" not in entry:
            raise CLIError(f"Invalid tag (expected key=value): {entry}")
        key, value = entry.split("=", 1)
        metadata[key.strip()] = value.strip()
    return metadata


def build_create_payload(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.project:
        raise CLIError("Project id is required (--project or DEFAULT_PROJECT_ID).")
    description = read_description(args.description)
    host = {
        "hostname": args.hostname,
        "provider": (args.host_provider or "").strip(),
        "region": (args.host_region or "").strip(),
        "ip": (args.host_ip or "").strip(),
        "notes": (args.host_notes or "").strip(),
    }
    owner = {
        "name": args.owner_name,
        "email": (args.owner_email or "").strip(),
        "slack": (args.owner_slack or "").strip(),
        "role": (args.owner_role or "").strip(),
    }
    payload: Dict[str, Any] = {
        "project_id": args.project,
        "slug": args.slug,
        "name": args.name,
        "description": description,
        "host": host,
        "owner": owner,
    }
    lifecycle = {"state": args.state, "notes": (args.lifecycle_notes or "").strip()}
    payload["lifecycle"] = lifecycle
    health = {
        "status": args.health_status,
        "url": (args.health_url or "").strip(),
        "notes": (args.health_notes or "").strip(),
    }
    if args.health_checked_at:
        health["checked_at"] = args.health_checked_at.strip()
    payload["health"] = health
    ports = parse_ports(args.ports)
    if ports:
        payload["ports"] = ports
    metadata = parse_tags(args.tags)
    if metadata:
        payload["metadata"] = metadata
    return payload


def build_update_payload(
    args: argparse.Namespace,
    current: Dict[str, Any],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if args.project:
        payload["project_id"] = args.project
    if args.slug:
        payload["slug"] = args.slug
    if args.name:
        payload["name"] = args.name
    if args.description is not None:
        payload["description"] = args.description.strip()
    host_fields = [args.hostname, args.host_ip, args.host_provider, args.host_region, args.host_notes]
    if any(field is not None for field in host_fields):
        host = dict(current.get("host") or {})
        if args.hostname is not None:
            host["hostname"] = args.hostname
        if args.host_ip is not None:
            host["ip"] = args.host_ip
        if args.host_provider is not None:
            host["provider"] = args.host_provider
        if args.host_region is not None:
            host["region"] = args.host_region
        if args.host_notes is not None:
            host["notes"] = args.host_notes
        payload["host"] = host
    owner_fields = [args.owner_name, args.owner_email, args.owner_slack, args.owner_role]
    if any(field is not None for field in owner_fields):
        owner = dict(current.get("owner") or {})
        if args.owner_name is not None:
            owner["name"] = args.owner_name
        if args.owner_email is not None:
            owner["email"] = args.owner_email
        if args.owner_slack is not None:
            owner["slack"] = args.owner_slack
        if args.owner_role is not None:
            owner["role"] = args.owner_role
        payload["owner"] = owner
    if args.state is not None or args.lifecycle_notes is not None:
        lifecycle = dict(current.get("lifecycle") or {})
        if args.state is not None:
            lifecycle["state"] = args.state
        if args.lifecycle_notes is not None:
            lifecycle["notes"] = args.lifecycle_notes
        payload["lifecycle"] = lifecycle
    if any(value is not None for value in (args.health_status, args.health_url, args.health_notes, args.health_checked_at)):
        health = dict(current.get("health") or {})
        if args.health_status is not None:
            health["status"] = args.health_status
        if args.health_url is not None:
            health["url"] = args.health_url
        if args.health_notes is not None:
            health["notes"] = args.health_notes
        if args.health_checked_at is not None:
            health["checked_at"] = args.health_checked_at
        payload["health"] = health
    if args.ports:
        payload["ports"] = parse_ports(args.ports)
    if args.tags:
        payload["metadata"] = parse_tags(args.tags)
    return payload


def fetch_environment(base_url: str, identifier: str, token: str, timeout: float) -> Dict[str, Any]:
    last_error: CLIError | None = None
    try:
        response = request_api(base_url, f"/api/environments/{identifier}", token=token, timeout=timeout)
        env = response.get("environment") or response
        if env:
            return env
    except CLIError as exc:
        if identifier.startswith("env-") or identifier.startswith("ENV-"):
            raise
        last_error = exc
    collection = request_api(base_url, "/api/environments", token=token, timeout=timeout)
    environments = collection.get("environments") or []
    for env in environments:
        if env.get("slug") == identifier:
            return env
    if last_error:
        raise last_error
    raise CLIError(f"Environment '{identifier}' not found")


def render_row(env: Dict[str, Any]) -> str:
    project = env.get("project", {}).get("id") or env.get("project_id")
    host = env.get("host", {}).get("hostname") or "unknown-host"
    lifecycle = (env.get("lifecycle", {}).get("state") or "?").capitalize()
    health = (env.get("health", {}).get("status") or "unknown").capitalize()
    return f"{env.get('environment_id')} · {env.get('slug')} · project={project} · host={host} · state={lifecycle} · health={health}"


def print_environment(env: Dict[str, Any]) -> None:
    project = env.get("project") or {}
    print(f"{env.get('name')} ({env.get('slug')})")
    print(f"  Environment ID : {env.get('environment_id')}")
    print(f"  Project        : {project.get('name') or env.get('project_id')}")
    host = env.get("host") or {}
    print(f"  Host           : {host.get('hostname')} ({host.get('provider') or 'unknown provider'})")
    if host.get("ip"):
        print(f"                   ip={host.get('ip')} region={host.get('region') or '-'}")
    owner = env.get("owner") or {}
    print(f"  Owner          : {owner.get('name')} ({owner.get('role') or 'role n/a'})")
    if owner.get("email") or owner.get("slack"):
        print(f"                   email={owner.get('email') or '-'} slack={owner.get('slack') or '-'}")
    lifecycle = env.get("lifecycle") or {}
    print(
        "  Lifecycle      : "
        f"{(lifecycle.get('state') or 'unknown').capitalize()}"
        f" (updated {lifecycle.get('changed_at') or 'n/a'})"
    )
    if lifecycle.get("notes"):
        print(f"                   notes={lifecycle.get('notes')}")
    health = env.get("health") or {}
    print(
        "  Health         : "
        f"{(health.get('status') or 'unknown').capitalize()}"
        f" (last check: {health.get('checked_at') or 'n/a'})"
    )
    if health.get("url"):
        print(f"                   url={health.get('url')}")
    if health.get("notes"):
        print(f"                   notes={health.get('notes')}")
    description = (env.get("description") or "").strip()
    if description:
        print("  Description    :")
        for line in description.splitlines():
            print(f"                   {line}")
    ports = env.get("ports") or []
    if ports:
        print("  Ports          :")
        for port in ports:
            desc = f"{port.get('name')}:{port.get('port')}/{port.get('protocol')}"
            if port.get("url"):
                desc += f" → {port.get('url')}"
            if port.get("description"):
                desc += f" ({port.get('description')})"
            print(f"                   {desc}")
    metadata = env.get("metadata") or {}
    if metadata:
        print("  Metadata       :")
        for key, value in metadata.items():
            print(f"                   {key}={value}")


def handle_list(args: argparse.Namespace, base_url: str, token: str) -> None:
    query = ""
    if args.project:
        query = f"?project_id={args.project}"
    response = request_api(base_url, f"/api/environments{query}", token=token, timeout=args.timeout)
    if args.json:
        print(json.dumps(response, indent=2))
        return
    environments = response.get("environments") or []
    if not environments:
        print("No environments found.")
        return
    for env in environments:
        print(render_row(env))


def handle_show(args: argparse.Namespace, base_url: str, token: str) -> None:
    env = fetch_environment(base_url, args.identifier, token, args.timeout)
    if args.json:
        print(json.dumps(env, indent=2))
        return
    print_environment(env)


def handle_create(args: argparse.Namespace, base_url: str, token: str) -> None:
    payload = build_create_payload(args)
    response = request_api(base_url, "/api/environments", method="POST", payload=payload, token=token, timeout=args.timeout)
    env = response.get("environment") or response
    print(f"Created {env.get('environment_id')} ({env.get('slug')})")


def handle_update(args: argparse.Namespace, base_url: str, token: str) -> None:
    env = fetch_environment(base_url, args.identifier, token, args.timeout)
    payload = build_update_payload(args, env)
    if not payload:
        raise CLIError("No changes provided.")
    env_id = env.get("environment_id")
    response = request_api(
        base_url,
        f"/api/environments/{env_id}",
        method="PUT",
        payload=payload,
        token=token,
        timeout=args.timeout,
    )
    updated = response.get("environment") or response
    print(f"Updated {updated.get('environment_id')} ({updated.get('slug')})")


def handle_delete(args: argparse.Namespace, base_url: str, token: str) -> None:
    env = fetch_environment(base_url, args.identifier, token, args.timeout)
    env_id = env.get("environment_id")
    request_api(
        base_url,
        f"/api/environments/{env_id}",
        method="DELETE",
        payload=None,
        token=token,
        timeout=args.timeout,
    )
    print(f"Deleted {env_id}")


def main() -> None:
    args = parse_args()
    base_url = build_base_url(args)
    token = ensure_token(args, base_url)
    try:
        if args.command == "list":
            handle_list(args, base_url, token)
        elif args.command == "show":
            handle_show(args, base_url, token)
        elif args.command == "create":
            handle_create(args, base_url, token)
        elif args.command == "update":
            handle_update(args, base_url, token)
        elif args.command == "delete":
            handle_delete(args, base_url, token)
        else:  # pragma: no cover - defensive
            raise CLIError(f"Unknown command: {args.command}")
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
