"""Monitor Cloudflare tunnel readiness for Nightshift."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error as urlerror
from urllib import request


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CloudflareTunnelMonitor(threading.Thread):
    """Poll the cloudflared readiness endpoint and surface tunnel status."""

    def __init__(
        self,
        ready_url: str,
        *,
        interval_seconds: float = 5.0,
        timeout_seconds: float = 2.0,
        required: bool = True,
        lan_override_enabled: bool = False,
        lan_override_path: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        super().__init__(daemon=True)
        self.ready_url = ready_url
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self.required = required
        self._lan_override_env = lan_override_enabled
        self._lan_override_path = Path(lan_override_path) if lan_override_path else None
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._status: Dict[str, Any] = {
            "required": bool(required),
            "healthy": False,
            "lan_mode": bool(lan_override_enabled),
            "ready_connections": 0,
            "last_checked_at": None,
            "last_healthy_at": None,
            "last_error": None,
            "ready_url": ready_url,
            "lan_mode_override_path": str(self._lan_override_path) if self._lan_override_path else None,
        }

    # ------------------------------------------------------------------ lifecycle
    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self._poll_once()
            self._stop_event.wait(self.interval_seconds)

    # ------------------------------------------------------------------ helpers
    def _read_override_flag(self) -> bool:
        if self._lan_override_env:
            return True
        if not self._lan_override_path:
            return False
        try:
            if not self._lan_override_path.exists():
                return False
            text = self._lan_override_path.read_text(encoding="utf-8")
        except OSError:
            return False
        normalized = (text or "").strip().lower()
        if not normalized:
            return True
        return normalized in {"1", "true", "yes", "allow", "lan"}

    def _poll_once(self) -> None:
        now = _utcnow_iso()
        lan_mode = self._read_override_flag()
        healthy = False
        ready_connections = 0
        connector_id: Optional[str] = None
        last_error: Optional[Dict[str, Any]] = None
        try:
            req = request.Request(self.ready_url, method="GET")
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                payload = response.read().decode("utf-8")
                if payload:
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        data = {}
                else:
                    data = {}
                ready_connections = int(data.get("readyConnections") or 0)
                connector_id = data.get("connectorId")
                healthy = response.status == 200 and ready_connections > 0
        except urlerror.URLError as exc:
            healthy = False
            last_error = {"message": str(exc.reason or exc), "timestamp": now}
        except Exception as exc:  # pragma: no cover - defensive guard
            healthy = False
            last_error = {"message": str(exc), "timestamp": now}
        if healthy:
            last_healthy = now
            last_error = None
        else:
            last_healthy = self._status.get("last_healthy_at")
        self._status = {
            "required": self.required,
            "healthy": healthy,
            "lan_mode": lan_mode,
            "ready_connections": ready_connections,
            "connector_id": connector_id,
            "last_checked_at": now,
            "last_healthy_at": last_healthy,
            "last_error": last_error,
            "ready_url": self.ready_url,
            "lan_mode_override_path": str(self._lan_override_path) if self._lan_override_path else None,
        }
        if not healthy and not lan_mode and self.required:
            self._logger.warning("Cloudflare tunnel heartbeat failing (ready=%s)", ready_connections)

    # ------------------------------------------------------------------ public surface
    def status_payload(self) -> Dict[str, Any]:
        return dict(self._status)

    def is_healthy(self) -> bool:
        return bool(self._status.get("healthy"))

    def lan_mode_enabled(self) -> bool:
        return bool(self._status.get("lan_mode"))
