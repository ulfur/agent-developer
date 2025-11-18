#!/usr/bin/env python3
"""Trigger footer-right refreshes and time how long the panel takes to update."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Optional
from urllib import error, request

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.auth_tokens import issue_service_token

DEFAULT_BACKEND_URL = os.environ.get("NIGHTSHIFT_BACKEND_URL", "http://127.0.0.1:8080")
DEFAULT_DATA_DIR = REPO_ROOT / "data"
PROGRESS_LOG = REPO_ROOT / "logs" / "progress.log"
LOG_MARKER = "Footer right refresh duration_ms="


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", default=DEFAULT_BACKEND_URL, help="Nightshift backend base URL")
    parser.add_argument("--auth-email", default=None, help="Service account email (defaults to first user)")
    parser.add_argument("--auth-token", default=None, help="Reuse an existing Bearer token")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Path to data/ for auth secrets")
    parser.add_argument("--text", default="Yes boss?", help="Footer text to display")
    parser.add_argument("--duration", type=float, default=3.0, help="Seconds to keep the override active")
    parser.add_argument("--iterations", type=int, default=5, help="How many refreshes to run")
    parser.add_argument("--timeout", type=float, default=5.0, help="Seconds to wait for each measurement")
    return parser.parse_args()


def ensure_token(args: argparse.Namespace) -> str:
    if args.auth_token:
        return args.auth_token
    data_dir = args.data_dir if args.data_dir.is_absolute() else REPO_ROOT / args.data_dir
    return issue_service_token(data_dir, args.auth_email)


def post_footer_message(base_url: str, token: str, text: str, duration: float) -> None:
    payload = json.dumps({"text": text, "duration_sec": duration}).encode("utf-8")
    url = f"{base_url.rstrip('/')}/api/eink/footer_message"
    req = request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with request.urlopen(req, timeout=3.0) as resp:
        resp.read()


def read_new_logs(offset: int) -> tuple[int, list[str]]:
    PROGRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    size = PROGRESS_LOG.stat().st_size if PROGRESS_LOG.exists() else 0
    if size <= offset:
        return offset, []
    with PROGRESS_LOG.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
    new_offset = offset + len(chunk)
    text = chunk.decode("utf-8", errors="ignore")
    return new_offset, [line.strip() for line in text.splitlines() if line.strip()]


def wait_for_measurement(initial_offset: int, timeout: float) -> tuple[int, Optional[str]]:
    offset = initial_offset
    deadline = time.time() + timeout
    while time.time() < deadline:
        offset, lines = read_new_logs(offset)
        for line in lines:
            if LOG_MARKER in line:
                return offset, line
        time.sleep(0.1)
    return offset, None


def main() -> int:
    args = parse_args()
    token = ensure_token(args)
    backend = args.backend_url.rstrip("/")
    offset = PROGRESS_LOG.stat().st_size if PROGRESS_LOG.exists() else 0
    print(f"Testing footer refresh via {backend} ({args.iterations} iterations)\n")
    for idx in range(1, args.iterations + 1):
        try:
            post_footer_message(backend, token, args.text, args.duration)
        except (error.URLError, TimeoutError) as exc:
            print(f"[{idx}] API request failed: {exc}")
            break
        offset, line = wait_for_measurement(offset, args.timeout)
        if line:
            print(f"[{idx}] {line}")
        else:
            print(f"[{idx}] Timed out waiting for refresh log entry (>{args.timeout}s)\n")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
