#!/usr/bin/env python3
"""CLI helper to enqueue prompts without using the web UI."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any, Dict
from urllib import error, request


DEFAULT_HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("AGENT_PORT", "8080"))
DEFAULT_API_URL = os.environ.get("AGENT_API_URL")
DEFAULT_PROJECT = os.environ.get("DEFAULT_PROJECT_ID")
DEFAULT_EMAIL = os.environ.get("AGENT_EMAIL")
DEFAULT_PASSWORD = os.environ.get("AGENT_PASSWORD")
DEFAULT_TOKEN = os.environ.get("AGENT_TOKEN")
DEFAULT_TIMEOUT = float(os.environ.get("AGENT_API_TIMEOUT", "10"))


class CLIError(Exception):
    """Raised when the helper cannot queue a prompt."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Queue a prompt via the Agent Dev Host backend API.",
        epilog=(
            "Provide the prompt text as an argument or pipe it via stdin. "
            "Credentials can be supplied via --email/--password or the "
            "AGENT_EMAIL/AGENT_PASSWORD env vars."
        ),
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Prompt text. If omitted, the script reads from stdin.",
    )
    parser.add_argument(
        "-p",
        "--project",
        default=DEFAULT_PROJECT,
        help="Project id to associate with the prompt (default: %(default)s).",
    )
    parser.add_argument(
        "--url",
        dest="base_url",
        default=DEFAULT_API_URL,
        help="Override the API base URL (e.g. https://pi.local).",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Backend host when --url is not set (default: %(default)s).",
    )
    parser.add_argument(
        "--port",
        default=DEFAULT_PORT,
        type=int,
        help="Backend port when --url is not set (default: %(default)s).",
    )
    parser.add_argument(
        "-e",
        "--email",
        default=DEFAULT_EMAIL,
        help="Login email (default: %(default)s).",
    )
    parser.add_argument(
        "--password",
        default=DEFAULT_PASSWORD,
        help="Login password. If omitted you will be prompted.",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN,
        help="Existing bearer token (skips the login call).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Request timeout in seconds (default: %(default)s).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print the prompt id on success.",
    )
    return parser.parse_args()


def build_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        return args.base_url.rstrip("/")
    return f"http://{args.host}:{args.port}".rstrip("/")


def read_prompt_text(arg_value: str | None) -> str:
    if arg_value:
        text = arg_value
    else:
        if sys.stdin.isatty():
            print("Enter prompt text, then press Ctrl-D (EOF) to submit:", file=sys.stderr)
        text = sys.stdin.read()
    cleaned = text.strip()
    if not cleaned:
        raise CLIError("Prompt text cannot be empty.")
    return cleaned


def request_json(url: str, payload: Dict[str, Any], token: str | None, timeout: float) -> Dict[str, Any]:
    encoded = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "agent-cli/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return json.loads(body)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8")
        try:
            msg = json.loads(detail).get("error") or detail
        except json.JSONDecodeError:
            msg = detail or exc.reason
        raise CLIError(f"API request failed ({exc.code}): {msg}") from None
    except error.URLError as exc:
        raise CLIError(f"Unable to reach {url}: {exc.reason}") from None


def login(base_url: str, email: str | None, password: str | None, timeout: float) -> str:
    if not email:
        email = input("Email: ").strip()
    if not email:
        raise CLIError("Email is required.")
    if not password:
        password = getpass.getpass("Password: ")
    payload = {"email": email, "password": password}
    response = request_json(f"{base_url}/api/login", payload, token=None, timeout=timeout)
    token = response.get("token")
    if not token:
        raise CLIError("Login succeeded but no token was returned.")
    return token


def enqueue_prompt(base_url: str, token: str, prompt: str, project_id: str | None, timeout: float) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"prompt": prompt}
    if project_id:
        payload["project_id"] = project_id
    response = request_json(f"{base_url}/api/prompts", payload, token=token, timeout=timeout)
    if "prompt_id" not in response:
        raise CLIError("Response did not include a prompt_id.")
    return response


def main() -> int:
    args = parse_args()
    try:
        base_url = build_base_url(args)
        prompt_text = read_prompt_text(args.prompt)
        token = args.token
        if not token:
            token = login(base_url, args.email, args.password, args.timeout)
        result = enqueue_prompt(base_url, token, prompt_text, args.project, args.timeout)
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    prompt_id = result.get("prompt_id")
    status = result.get("status")
    if args.quiet:
        print(prompt_id)
    else:
        print(f"Queued prompt {prompt_id} (status: {status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

