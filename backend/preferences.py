"""Lightweight preference store used by the backend."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict


class PreferenceStore:
    """Persist small operator preferences such as the selected theme."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw or "{}")
            if isinstance(data, dict):
                self._data = data
            else:
                self._data = {}
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _persist(self) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self._data, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)

    def get_theme_mode(self) -> str:
        """Return the stored theme preference (“light” or “dark”)."""
        with self._lock:
            stored = str(self._data.get("theme_mode") or "").lower()
        return stored if stored in {"light", "dark"} else "dark"

    def set_theme_mode(self, mode: str) -> str:
        """Persist the requested theme preference."""
        normalized = (mode or "").strip().lower()
        if normalized not in {"light", "dark"}:
            raise ValueError("mode must be 'light' or 'dark'")
        with self._lock:
            if self._data.get("theme_mode") == normalized:
                return normalized
            self._data["theme_mode"] = normalized
            self._persist()
        return normalized
