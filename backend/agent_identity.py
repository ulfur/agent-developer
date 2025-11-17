"""Manage agent identity pairing and control-plane sync."""

from __future__ import annotations

import json
import logging
import secrets
import string
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error as urlerror
from urllib import request

import yaml

DEFAULT_CONTROL_PLANE_BASE_URL = "https://api.nghtshft.ai"
DEFAULT_PAIRING_INSTRUCTIONS_URL = "https://nghtshft.ai/pair"
PAIRING_CODE_LENGTH = 4
PAIRING_CODE_GROUPS = 2


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    return {}


def _clean_str_list(value: Any) -> list[str]:
    items: list[str] = []
    if isinstance(value, (list, tuple, set)):
        iterator = value
    elif value is None or value == "":
        iterator = []
    else:
        iterator = [value]
    for entry in iterator:
        text = _clean_str(entry)
        if text:
            items.append(text)
    return items


class AgentIdentityError(Exception):
    """Raised when the identity bundle is invalid or missing."""


class AgentIdentityManager:
    """Persist and expose the agent identity bundle."""

    def __init__(
        self,
        identity_path: Path,
        pairing_state_path: Path,
        *,
        logger: Optional[logging.Logger] = None,
        control_plane_base_url: Optional[str] = None,
        pairing_instructions_url: Optional[str] = None,
        http_timeout: float = 10.0,
    ) -> None:
        self.identity_path = Path(identity_path)
        self.pairing_state_path = Path(pairing_state_path)
        self.logger = logger or logging.getLogger(__name__)
        self.control_plane_base_url = _clean_str(control_plane_base_url) or DEFAULT_CONTROL_PLANE_BASE_URL
        self.pairing_instructions_url = pairing_instructions_url or DEFAULT_PAIRING_INSTRUCTIONS_URL
        self.http_timeout = http_timeout
        self._lock = threading.Lock()
        self._identity: Dict[str, Any] = {}
        self._pairing_state: Dict[str, Any] = {}
        self._last_sync_error: Optional[Dict[str, Any]] = None
        self.identity_path.parent.mkdir(parents=True, exist_ok=True)
        self.pairing_state_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_identity()
        self._load_pairing_state()
        if not self._identity:
            self._ensure_pairing_state()

    # ------------------------------------------------------------------ lifecycle
    def _load_identity(self) -> None:
        if not self.identity_path.exists():
            with self._lock:
                self._identity = {}
            return
        try:
            payload = yaml.safe_load(self.identity_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            payload = {}
        if not isinstance(payload, dict) or not self._looks_like_identity(payload):
            payload = {}
        with self._lock:
            self._identity = payload

    def _persist_identity(self) -> None:
        serialized = yaml.safe_dump(self._identity, indent=2, sort_keys=False)
        tmp_path = self.identity_path.with_suffix(".tmp")
        tmp_path.write_text(serialized, encoding="utf-8")
        tmp_path.replace(self.identity_path)

    def _load_pairing_state(self) -> None:
        if not self.pairing_state_path.exists():
            with self._lock:
                self._pairing_state = {}
            return
        try:
            payload = json.loads(self.pairing_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        with self._lock:
            self._pairing_state = payload

    def _persist_pairing_state(self) -> None:
        tmp_path = self.pairing_state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self._pairing_state, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.pairing_state_path)

    def _ensure_pairing_state(self) -> None:
        with self._lock:
            if self._identity:
                return
            current_code = _clean_str(self._pairing_state.get("pairing_code"))
            if current_code:
                return
            code = self._generate_pairing_code()
            state = {
                "pairing_code": code,
                "instructions_url": self.pairing_instructions_url,
                "created_at": utcnow_iso(),
            }
            self._pairing_state = state
            self._persist_pairing_state()
        self.logger.warning(
            "Agent identity missing. Visit %s and enter pairing code %s to finish setup.",
            self.pairing_instructions_url,
            code,
        )

    def trigger_pairing_reset(self) -> None:
        """Force a new pairing code."""
        with self._lock:
            self._identity = {}
            self._last_sync_error = None
            try:
                self.identity_path.unlink()
            except FileNotFoundError:
                pass
        self._ensure_pairing_state()

    # ------------------------------------------------------------------ properties
    def is_paired(self) -> bool:
        with self._lock:
            return bool(self._identity)

    def pairing_payload(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._pairing_state)

    def identity_bundle(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._identity))

    # ------------------------------------------------------------------ pairing
    def accept_registration(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise AgentIdentityError("registration payload must be a JSON object")
        code = _clean_str(payload.get("pairing_code"))
        if not code:
            raise AgentIdentityError("pairing_code is required")
        with self._lock:
            expected_code = _clean_str(self._pairing_state.get("pairing_code"))
        if not expected_code or expected_code != code:
            raise AgentIdentityError("invalid or expired pairing code")
        normalized = self._normalize_bundle(payload)
        with self._lock:
            self._identity = normalized
            self._last_sync_error = None
            self._persist_identity()
            self._pairing_state = {}
            try:
                self.pairing_state_path.unlink()
            except FileNotFoundError:
                pass
        agent = normalized.get("agent", {})
        self.logger.info(
            "Registered agent %s (%s)",
            agent.get("name") or agent.get("id") or "unknown",
            agent.get("id") or "unknown",
        )
        return normalized

    def _normalize_bundle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = utcnow_iso()
        agent_payload = _clean_dict(payload.get("agent"))
        agent_id = _clean_str(agent_payload.get("id") or payload.get("agent_id"))
        if not agent_id:
            raise AgentIdentityError("agent.id is required")
        control_plane_payload = _clean_dict(payload.get("control_plane"))
        api_base = _clean_str(control_plane_payload.get("api_base") or payload.get("control_plane_api"))
        auth_token = _clean_str(control_plane_payload.get("auth_token") or payload.get("auth_token"))
        if not auth_token:
            raise AgentIdentityError("control-plane auth token missing from payload")
        permissions_payload = _clean_dict(payload.get("permissions"))
        repo_permissions = permissions_payload.get("repos")
        workspace_permissions = permissions_payload.get("workspaces")
        normalized = {
            "agent": {
                "id": agent_id,
                "name": _clean_str(agent_payload.get("name") or agent_id),
                "branch": _clean_str(agent_payload.get("branch") or payload.get("branch")),
                "hardware": _clean_str(agent_payload.get("hardware") or payload.get("hardware")),
                "workspace": _clean_str(agent_payload.get("workspace") or payload.get("workspace")),
            },
            "control_plane": {
                "api_base": api_base or self.control_plane_base_url,
                "auth_token": auth_token,
                "paired_at": control_plane_payload.get("paired_at") or now,
                "last_synced_at": control_plane_payload.get("last_synced_at") or now,
                "device_id": _clean_str(control_plane_payload.get("device_id") or payload.get("device_id")),
            },
            "permissions": {
                "repos": self._normalize_allowance(repo_permissions or payload.get("repo_permissions")),
                "workspaces": self._normalize_allowance(workspace_permissions or payload.get("workspace_permissions")),
            },
            "pm": _clean_dict(payload.get("pm")),
            "cloudflare": _clean_dict(payload.get("cloudflare")),
            "persona": _clean_dict(payload.get("persona")),
            "context": _clean_dict(payload.get("context")),
            "secrets": _clean_dict(payload.get("secrets")),
            "received_at": now,
        }
        return normalized

    @staticmethod
    def _normalize_allowance(value: Any) -> Dict[str, list[str]]:
        payload = _clean_dict(value)
        return {
            "allow": _clean_str_list(payload.get("allow")),
            "deny": _clean_str_list(payload.get("deny")),
        }

    @staticmethod
    def _looks_like_identity(payload: Dict[str, Any]) -> bool:
        agent = payload.get("agent")
        control_plane = payload.get("control_plane")
        if not isinstance(agent, dict) or not _clean_str(agent.get("id")):
            return False
        if not isinstance(control_plane, dict):
            return False
        auth_token = control_plane.get("auth_token")
        # Allow legacy bundles that might not expose auth_token but still have control-plane metadata.
        if auth_token is None:
            return True
        return bool(_clean_str(auth_token))

    def _generate_pairing_code(self) -> str:
        alphabet = string.ascii_uppercase + string.digits
        groups = []
        for _ in range(PAIRING_CODE_GROUPS):
            group = "".join(secrets.choice(alphabet) for _ in range(PAIRING_CODE_LENGTH))
            groups.append(group)
        return "-".join(groups)

    # ------------------------------------------------------------------ control plane sync
    def refresh_remote_config(self) -> bool:
        with self._lock:
            identity = json.loads(json.dumps(self._identity))
        if not identity:
            raise AgentIdentityError("agent identity not registered")
        agent = _clean_dict(identity.get("agent"))
        control_plane = _clean_dict(identity.get("control_plane"))
        agent_id = _clean_str(agent.get("id"))
        if not agent_id:
            raise AgentIdentityError("agent id missing from identity bundle")
        api_base = _clean_str(control_plane.get("api_base")) or self.control_plane_base_url
        auth_token = _clean_str(control_plane.get("auth_token"))
        if not auth_token:
            raise AgentIdentityError("control-plane auth token missing from identity bundle")
        url = f"{api_base.rstrip('/')}/agent/{agent_id}/config"
        try:
            remote_payload = self._fetch_remote_config(url, auth_token)
        except AgentIdentityError as exc:
            self._record_sync_error(str(exc))
            raise
        merged = self._merge_identity(identity, remote_payload)
        merged.setdefault("control_plane", control_plane)
        merged["control_plane"]["api_base"] = api_base
        merged["control_plane"]["auth_token"] = auth_token
        merged["control_plane"]["last_synced_at"] = utcnow_iso()
        with self._lock:
            self._identity = merged
            self._last_sync_error = None
            self._persist_identity()
        self.logger.info(
            "Control plane config refreshed for %s (%s)",
            agent.get("name") or agent_id,
            agent_id,
        )
        return True

    def _fetch_remote_config(self, url: str, token: str) -> Dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "nightshift-agent/1.0",
        }
        req = request.Request(url, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=self.http_timeout) as response:
                raw = response.read().decode("utf-8")
        except urlerror.URLError as exc:  # pragma: no cover - network failure
            raise AgentIdentityError(f"control plane request failed: {exc}") from exc
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise AgentIdentityError("control plane returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AgentIdentityError("control plane returned unexpected payload")
        config = payload.get("config")
        if isinstance(config, dict):
            return config
        return payload

    def _merge_identity(self, current: Dict[str, Any], remote: Dict[str, Any]) -> Dict[str, Any]:
        merged = json.loads(json.dumps(current))
        if not isinstance(remote, dict):
            return merged
        for key, value in remote.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    def _record_sync_error(self, message: str) -> None:
        self._last_sync_error = {"message": message, "timestamp": utcnow_iso()}
        self.logger.warning("Control plane sync failed: %s", message)

    # ------------------------------------------------------------------ payloads
    def public_payload(self) -> Dict[str, Any]:
        with self._lock:
            identity = json.loads(json.dumps(self._identity))
            pairing_state = dict(self._pairing_state)
            last_error = dict(self._last_sync_error) if self._last_sync_error else None
        if not identity:
            return {
                "status": "pairing",
                "pairing_code": pairing_state.get("pairing_code"),
                "instructions_url": pairing_state.get("instructions_url") or self.pairing_instructions_url,
                "created_at": pairing_state.get("created_at"),
            }
        agent = _clean_dict(identity.get("agent"))
        control_plane = _clean_dict(identity.get("control_plane"))
        permissions = _clean_dict(identity.get("permissions"))
        cloudflare = _clean_dict(identity.get("cloudflare"))
        persona = _clean_dict(identity.get("persona"))
        payload = {
            "status": "paired",
            "agent": {
                "id": agent.get("id"),
                "name": agent.get("name"),
                "hardware": agent.get("hardware"),
                "workspace": agent.get("workspace"),
                "branch": agent.get("branch"),
            },
            "control_plane": {
                "api_base": control_plane.get("api_base") or self.control_plane_base_url,
                "paired_at": control_plane.get("paired_at"),
                "last_synced_at": control_plane.get("last_synced_at"),
                "device_id": control_plane.get("device_id"),
            },
            "cloudflare": {
                "hostname": cloudflare.get("hostname"),
                "tunnel_id": cloudflare.get("tunnel_id"),
                "account_id": cloudflare.get("account_id"),
            },
            "permissions": {
                "repos": permissions.get("repos"),
                "workspaces": permissions.get("workspaces"),
            },
            "persona": {
                "summary": persona.get("summary"),
                "profile": persona.get("profile"),
            },
            "context": identity.get("context") or {},
        }
        if last_error:
            payload["sync_error"] = last_error
        return payload
