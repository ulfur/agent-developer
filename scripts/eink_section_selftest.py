#!/usr/bin/env python3
"""Trigger the e-ink section self-test via the backend API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib import error, request

from lib.auth_tokens import issue_service_token

DEFAULT_BACKEND_URL = os.environ.get("NIGHTSHIFT_BACKEND_URL", "http://127.0.0.1:8080")
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", default=DEFAULT_BACKEND_URL, help="Base URL for the running backend")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Path to data/ for auth secret")
    parser.add_argument("--email", default=None, help="Service account email (defaults to first user)")
    parser.add_argument("--token", default=None, help="Pre-issued bearer token (skip local secret lookup)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = args.backend_url.rstrip("/")
    token = args.token or issue_service_token(args.data_dir, args.email)
    url = f"{base_url}/api/eink/selftest"
    print(f"Requesting section self-test via {url} ...")
    try:
        _post_json(url, {}, token)
    except error.URLError as exc:
        print(f"Self-test request failed: {exc}")
        return 1
    print("Self-test started. Watch the aux display for labeled rectangles.")
    return 0


def _post_json(url: str, payload: dict, token: str) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with request.urlopen(req, timeout=5) as resp:
        resp.read()


if __name__ == "__main__":
    raise SystemExit(main())
