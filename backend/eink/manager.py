"""Background worker that renders queue status to the e-ink display."""

from __future__ import annotations

import datetime as dt
import logging
import queue
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Tuple

from .it8591 import DisplayUnavailable, IT8591Config, IT8591DisplayDriver
from .renderer import StatusRenderer
from log_utils import extract_stdout_preview


class TaskQueueDisplayManager(threading.Thread):
    """Async worker that keeps the IT8591 panel in sync with the queue."""

    def __init__(
        self,
        store: Any,
        logger: logging.Logger,
        enabled: bool,
        config: IT8591Config,
        max_items: int = 5,
        preferences: Any | None = None,
        human_tasks: Any | None = None,
    ):
        super().__init__(daemon=True)
        self.store = store
        self.logger = logger
        self.enabled = enabled
        self.config = config
        self.max_items = max_items
        self.preferences = preferences
        self.human_tasks = human_tasks
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=2)
        self._stop = threading.Event()
        self._driver: IT8591DisplayDriver | None = None
        self._renderer: StatusRenderer | None = None
        self._last_success: dt.datetime | None = None
        self._init_failed_at: dt.datetime | None = None

    def request_refresh(self, reason: str = "") -> None:
        """Queue a refresh request if the subsystem is enabled."""
        if not self.enabled:
            return
        if self._driver is None and not self._ensure_driver():
            return
        try:
            self._queue.put_nowait(reason or "update")
        except queue.Full:
            # coalesce multiple requests to avoid overwhelming the HAT
            pass

    def run(self) -> None:
        if not self.enabled:
            self.logger.info("E-ink display disabled via configuration")
            return
        self.logger.info("Starting e-ink display manager thread")
        # Kick off an initial refresh once the driver is ready.
        self.request_refresh("initial")
        while not self._stop.is_set():
            if self._driver is None:
                # Allow a retry every 30s if the first initialisation failed.
                if self._init_failed_at and (dt.datetime.utcnow() - self._init_failed_at).total_seconds() < 30:
                    time.sleep(5)
                else:
                    self._ensure_driver()
                time.sleep(0.1)
                continue
            try:
                _ = self._queue.get(timeout=2)
            except queue.Empty:
                continue
            self._refresh_panel()

    def stop(self) -> None:
        self._stop.set()
        self.request_refresh("shutdown")
        if self._driver:
            self._driver.close()

    # ---------------------------------------------------------------- helpers
    def _ensure_driver(self) -> bool:
        if self._driver is not None:
            return True
        try:
            self._driver = IT8591DisplayDriver(self.config, self.logger)
            self._renderer = StatusRenderer(self._driver.width, self._driver.height)
            self.logger.info(
                "Initialised IT8591 e-ink display (%sx%s px)",
                self._driver.width,
                self._driver.height,
            )
            self._init_failed_at = None
            return True
        except DisplayUnavailable as exc:
            self.logger.warning("E-ink display unavailable: %s", exc)
            self._driver = None
            self._renderer = None
            self._init_failed_at = dt.datetime.utcnow()
            return False

    def _refresh_panel(self) -> None:
        if not self._driver or not self._renderer:
            return
        try:
            snapshot = self.store.list_prompts()
            human_records, human_summary = self._collect_human_tasks()
            entries = self._build_display_entries(snapshot.get("items", []), human_records)
            queue_depth = self.store.pending_count()
            human_notifications = self._calculate_human_notifications(human_summary)
            invert = self._should_invert_display()
            image = self._renderer.render(
                entries,
                invert=invert,
                pending_count=queue_depth,
                human_notification_count=human_notifications,
            )
            self._driver.display_image(image)
            self._last_success = dt.datetime.utcnow()
            self.logger.info(
                "E-ink display updated with %s items (pending=%s, human_notifications=%s)",
                len(entries),
                queue_depth,
                human_notifications,
            )
        except Exception as exc:  # pragma: no cover - hardware path
            self.logger.exception("Failed to push update to e-ink display: %s", exc)

    def _collect_human_tasks(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        summary: Dict[str, Any] = {"blocking_count": 0, "status_counts": {}}
        if not self.human_tasks:
            return [], summary
        getter = getattr(self.human_tasks, "list_tasks", None)
        if not callable(getter):
            return [], summary
        try:
            records = getter()
        except Exception as exc:  # pragma: no cover - defensive logging
            self.logger.warning("Failed to read human tasks: %s", exc)
            return [], summary
        normalized: List[Dict[str, Any]] = []
        blocking_count = 0
        status_counts: Dict[str, int] = {}
        for record in records or []:
            entry = self._normalize_human_task_record(record)
            normalized.append(entry)
            status = str(entry.get("status") or "").lower()
            status_counts[status] = status_counts.get(status, 0) + 1
            if entry.get("blocking"):
                blocking_count += 1
        summary = {"blocking_count": blocking_count, "status_counts": status_counts}
        return normalized, summary

    def _build_display_entries(
        self,
        prompt_records: List[Dict[str, Any]],
        human_records: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for record in human_records:
            entries.append(self._format_human_task_entry(record))
            if len(entries) >= self.max_items:
                return entries
        for record in prompt_records:
            entries.append(self._format_prompt_entry(record))
            if len(entries) >= self.max_items:
                break
        return entries

    def _normalize_human_task_record(self, record: Any) -> Dict[str, Any]:
        if isinstance(record, dict):
            payload = dict(record)
        elif is_dataclass(record):
            payload = asdict(record)
        else:
            payload = {}
            for attr in (
                "task_id",
                "title",
                "description",
                "status",
                "blocking",
                "project_id",
                "created_at",
                "updated_at",
            ):
                if hasattr(record, attr):
                    payload[attr] = getattr(record, attr)
        payload.setdefault("status", "open")
        payload.setdefault("title", "Human task")
        payload.setdefault("description", "")
        if not payload.get("created_at") and payload.get("updated_at"):
            payload["created_at"] = payload["updated_at"]
        if not payload.get("updated_at") and payload.get("created_at"):
            payload["updated_at"] = payload["created_at"]
        return payload

    def _format_prompt_entry(self, record: Dict[str, Any]) -> Dict[str, Any]:
        enriched = dict(record)
        if record.get("status") == "completed":
            enriched["stdout_preview"] = extract_stdout_preview(record.get("log_path") or "")
        else:
            enriched["stdout_preview"] = ""
        enriched["entry_type"] = "agent"
        return enriched

    def _format_human_task_entry(self, record: Dict[str, Any]) -> Dict[str, Any]:
        status = str(record.get("status") or "open").lower()
        blocking = bool(record.get("blocking"))
        status_prefix = "human"
        if blocking:
            status_prefix = "blocker"
        entry_status = f"{status_prefix}_{status}"
        detail_text = self._build_human_task_detail(record, blocking=blocking)
        entry = {
            "status": entry_status,
            "text": detail_text,
            "created_at": record.get("created_at") or record.get("updated_at") or "",
            "updated_at": record.get("updated_at") or record.get("created_at") or "",
            "project_id": record.get("project_id"),
            "task_id": record.get("task_id"),
            "title": record.get("title"),
            "stdout_preview": "",
            "entry_type": "human",
        }
        return entry

    def _build_human_task_detail(self, record: Dict[str, Any], *, blocking: bool) -> str:
        title = str(record.get("title") or "").strip()
        description = str(record.get("description") or "").strip()
        pieces: List[str] = []
        if blocking:
            pieces.append("[BLOCKING]")
        if title:
            pieces.append(title)
        if description:
            pieces.append(description)
        detail = " ".join(pieces).strip()
        return detail or "Human task pending"

    def _should_invert_display(self) -> bool:
        if not self.preferences:
            return False
        getter = getattr(self.preferences, "get_theme_mode", None)
        if not callable(getter):
            return False
        try:
            return getter() == "dark"
        except Exception:
            return False

    def _calculate_human_notifications(self, summary: Dict[str, Any]) -> int:
        if not summary:
            return 0
        blocking = summary.get("blocking_count") or 0
        try:
            blocking_count = int(blocking)
        except (TypeError, ValueError):
            blocking_count = 0
        if blocking_count > 0:
            return blocking_count
        status_counts = summary.get("status_counts") or {}
        open_count = 0
        try:
            open_count = int(status_counts.get("open") or 0)
        except (TypeError, ValueError, AttributeError):
            open_count = 0
        return max(0, open_count)
