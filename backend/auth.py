"""Lightweight authentication helpers for the Agent Dev Host backend."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _urlsafe_b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


PBKDF2_ITERATIONS = 200_000
TOKEN_TTL_SECONDS = int(os.environ.get("AUTH_TOKEN_TTL", "43200"))  # 12 hours by default


@dataclass(frozen=True)
class AuthenticatedUser:
    email: str
    issued_at: int
    expires_at: int


class AuthManager:
    """Manages credential storage and stateless token issuance."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.users_path = self.data_dir / "users.json"
        self.secret_path = self.data_dir / ".auth_secret"
        self._users: Dict[str, dict] = {}
        self._secret = self._load_or_create_secret()
        self._load_users()

    # ------------------------------------------------------------------
    # User management helpers
    def _load_users(self) -> None:
        if not self.users_path.exists():
            self._persist_users()
        try:
            contents = self.users_path.read_text(encoding="utf-8")
            data = json.loads(contents or "{}")
        except json.JSONDecodeError:
            data = {}
        for entry in data.get("users", []):
            email = (entry.get("email") or "").strip()
            if not email:
                continue
            self._users[email.lower()] = entry

    def _persist_users(self) -> None:
        payload = {"users": list(self._users.values())}
        self.users_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def ensure_user(self, email: str, password: str) -> bool:
        """Creates the given user if it does not already exist."""
        key = email.lower()
        if key in self._users:
            return False
        user_entry = self._create_user_entry(email, password)
        self._users[key] = user_entry
        self._persist_users()
        return True

    def change_password(self, email: str, current_password: str, new_password: str) -> None:
        """Change the password for the given user after verifying the current password."""
        key = email.lower()
        record = self._users.get(key)
        if not record:
            raise ValueError("User not found")
        if not current_password or not new_password:
            raise ValueError("Current and new password are required")
        if current_password == new_password:
            raise ValueError("New password must be different from the current password")
        if len(new_password) < 8:
            raise ValueError("New password must be at least 8 characters")

        try:
            existing_salt = base64.b64decode(record["salt"])
            existing_hash = base64.b64decode(record["password_hash"])
        except (KeyError, ValueError, binascii.Error):  # type: ignore[name-defined]
            raise ValueError("Unable to verify the current password") from None

        candidate_hash = self._derive_hash(current_password, existing_salt)
        if not hmac.compare_digest(candidate_hash, existing_hash):
            raise ValueError("Current password is incorrect")

        updated_entry = self._create_user_entry(record["email"], new_password)
        updated_entry["created_at"] = record.get("created_at") or _utcnow_iso()
        updated_entry["updated_at"] = _utcnow_iso()
        self._users[key] = updated_entry
        self._persist_users()

    def _create_user_entry(self, email: str, password: str) -> dict:
        salt = secrets.token_bytes(16)
        password_hash = self._derive_hash(password, salt)
        return {
            "email": email,
            "salt": base64.b64encode(salt).decode("ascii"),
            "password_hash": base64.b64encode(password_hash).decode("ascii"),
            "created_at": _utcnow_iso(),
        }

    def _derive_hash(self, password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)

    def _load_or_create_secret(self) -> bytes:
        if self.secret_path.exists():
            return self.secret_path.read_bytes()
        secret = secrets.token_bytes(32)
        self.secret_path.write_bytes(secret)
        try:
            self.secret_path.chmod(0o600)
        except OSError:
            pass
        return secret

    # ------------------------------------------------------------------
    # Authentication + token helpers
    def authenticate(self, email: str, password: str) -> Optional[dict]:
        key = email.lower()
        record = self._users.get(key)
        if not record:
            return None
        try:
            salt = base64.b64decode(record["salt"])
            expected_hash = base64.b64decode(record["password_hash"])
        except (KeyError, ValueError, binascii.Error):  # type: ignore[name-defined]
            return None
        candidate_hash = self._derive_hash(password, salt)
        if not hmac.compare_digest(candidate_hash, expected_hash):
            return None
        return {"email": record["email"], "created_at": record.get("created_at")}

    def issue_token(self, email: str) -> str:
        issued_at = int(time.time())
        expires_at = issued_at + TOKEN_TTL_SECONDS
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {"sub": email, "iat": issued_at, "exp": expires_at}
        signing_input = ".".join(
            [
                _urlsafe_b64encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
                _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            ]
        ).encode("ascii")
        signature = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        token = b".".join([signing_input, _urlsafe_b64encode(signature).encode("ascii")])
        return token.decode("ascii")

    def verify_token(self, token: str) -> Optional[AuthenticatedUser]:
        try:
            header_b64, payload_b64, signature_b64 = token.split(".")
        except ValueError:
            return None
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        try:
            signature = _urlsafe_b64decode(signature_b64)
        except (ValueError, binascii.Error):  # type: ignore[name-defined]
            return None
        expected_sig = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected_sig):
            return None
        try:
            payload = json.loads(_urlsafe_b64decode(payload_b64))
        except json.JSONDecodeError:
            return None
        email = (payload.get("sub") or "").strip()
        if not email:
            return None
        key = email.lower()
        if key not in self._users:
            return None
        exp = payload.get("exp")
        iat = payload.get("iat")
        if not isinstance(exp, (int, float)) or time.time() > exp:
            return None
        if not isinstance(iat, (int, float)):
            return None
        return AuthenticatedUser(email=self._users[key]["email"], issued_at=int(iat), expires_at=int(exp))

    def user_payload(self, user: AuthenticatedUser | dict) -> dict:
        if isinstance(user, AuthenticatedUser):
            return {"email": user.email, "issued_at": user.issued_at, "expires_at": user.expires_at}
        return {"email": user.get("email"), "created_at": user.get("created_at")}


__all__ = ["AuthManager", "AuthenticatedUser"]
