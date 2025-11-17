"""Helper utilities for issuing service tokens from local auth data."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

__all__ = [
    "issue_service_token",
    "discover_default_email",
    "load_users",
]

_DEFAULT_AUTH_TOKEN_TTL = int(os.environ.get("AUTH_TOKEN_TTL", "43200"))


def _urlsafe_b64encode(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def load_users(data_dir: Path) -> Dict[str, str]:
    users_path = data_dir / "users.json"
    try:
        contents = users_path.read_text(encoding="utf-8")
        data = json.loads(contents or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    users: Dict[str, str] = {}
    for entry in data.get("users", []):
        email = str(entry.get("email") or "").strip()
        if email:
            users[email.lower()] = email
    return users


def discover_default_email(data_dir: Path) -> Optional[str]:
    users = load_users(data_dir)
    if not users:
        return None
    return next(iter(users.values()))


def issue_service_token(
    data_dir: Path,
    email: Optional[str],
    *,
    ttl: Optional[int] = None,
    secret_path: Optional[Path] = None,
) -> str:
    """Issue a JWT for the given user using the stored auth secret."""

    users = load_users(data_dir)
    target_email = (email or discover_default_email(data_dir) or "").strip()
    if not target_email:
        raise RuntimeError("No users available; unable to issue a service token")
    if target_email.lower() not in users:
        raise RuntimeError(f"User {target_email} does not exist in data/users.json")
    secret_file = secret_path or (data_dir / ".auth_secret")
    try:
        secret = secret_file.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"Unable to read {secret_file}: {exc}") from exc
    issued_at = int(time.time())
    token_ttl = int(ttl) if ttl is not None else _DEFAULT_AUTH_TOKEN_TTL
    payload = {
        "sub": users[target_email.lower()],
        "iat": issued_at,
        "exp": issued_at + token_ttl,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _urlsafe_b64encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = b".".join([header_b64, payload_b64])
    signature = _urlsafe_b64encode(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return b".".join([signing_input, signature]).decode("ascii")
