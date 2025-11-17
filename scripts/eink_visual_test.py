#!/usr/bin/env python3
"""Simple loop that exercises e-ink overlay and queue refresh modes via the backend API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import sys
import termios
import time
import tty
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence
from urllib import error, request

from lib.auth_tokens import issue_service_token

DEFAULT_BACKEND_URL = os.environ.get("NIGHTSHIFT_BACKEND_URL", "http://127.0.0.1:8080")
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
TEST_PATTERNS: tuple[tuple[str, Sequence[str], float, bool], ...] = (
    ("FAST: Listening", ["Fast-du overlay", "Expect update <2s"], 3.0, True),
    ("FAST: Transcribing", ["Grey text", "DU waveform"], 3.0, False),
    ("FAST: Transcript", ["Final line", "Should clear soon"], 3.0, True),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", default=DEFAULT_BACKEND_URL, help="Base URL for the running backend")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Path to data/ for auth secret")
    parser.add_argument("--email", default=None, help="Service account email (defaults to first user)")
    parser.add_argument("--token", default=None, help="Pre-issued bearer token (skip local secret lookup)")
    parser.add_argument("--cycles", type=int, default=1, help="How many times to loop through the overlay set")
    parser.add_argument("--pause", type=float, default=2.0, help="Seconds to wait between overlays")
    parser.add_argument("--clear-delay", type=float, default=3.0, help="Seconds to wait after clearing overlays")
    parser.add_argument("--wait-for-space", action="store_true", help="After each overlay, wait for SPACE to record actual display change time")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = args.backend_url.rstrip("/")
    token = args.token or issue_service_token(args.data_dir, args.email)
    for cycle in range(args.cycles):
        print(f"Cycle {cycle + 1}/{args.cycles}")
        for title, lines, duration, invert in TEST_PATTERNS:
            print(f"  - Overlay '{title}' for {duration}s", end="", flush=True)
            payload = {
                "title": title,
                "lines": list(lines),
                "duration_sec": duration,
            }
            if invert:
                payload["invert"] = True
            start = time.time()
            _post_json(f"{base_url}/api/eink/overlay", payload, token)
            elapsed = None
            if args.wait_for_space:
                print(" — press SPACE when the panel updates", end="", flush=True)
                elapsed = _wait_for_space()
            if elapsed is not None:
                print(f" (delta {elapsed:.2f}s)")
            else:
                print()
            time.sleep(duration + args.pause)
        print("  - Clearing overlay")
        _request(
            f"{base_url}/api/eink/overlay",
            token,
            method="DELETE",
        )
        time.sleep(args.clear_delay)
    print("Done.")
    return 0


def _post_json(url: str, payload: dict, token: str) -> None:
    data = json.dumps(payload).encode("utf-8")
    _request(url, token, data=data)


def _request(url: str, token: str, *, data: bytes | None = None, method: str | None = None) -> None:
    req = request.Request(url, data=data)
    if method:
        req.method = method
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with request.urlopen(req, timeout=5) as resp:
            resp.read()
    except error.URLError as exc:
        print(f"Request to {url} failed: {exc}")
        sys.exit(1)


@contextmanager
def _raw_mode(fileobj: object) -> Iterator[None]:
    fd = fileobj.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def _wait_for_space() -> float | None:
    start = time.time()
    with _raw_mode(sys.stdin):
        while True:
            ch = sys.stdin.read(1)
            if ch == " ":
                return time.time() - start
            if ch in {"\x03", "\x04"}:  # Ctrl-C / Ctrl-D
                raise KeyboardInterrupt
            # ignore other keys


if __name__ == "__main__":
    raise SystemExit(main())
