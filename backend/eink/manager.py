"""Background worker that renders queue status to the e-ink display."""

from __future__ import annotations

import datetime as dt
import logging
import queue
import threading
import time
from typing import Any, Dict, List

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
    ):
        super().__init__(daemon=True)
        self.store = store
        self.logger = logger
        self.enabled = enabled
        self.config = config
        self.max_items = max_items
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
            entries = self._build_display_entries(snapshot.get("items", []))
            queue_depth = self.store.pending_count()
            image = self._renderer.render(entries)
            self._driver.display_image(image)
            self._last_success = dt.datetime.utcnow()
            self.logger.info(
                "E-ink display updated with %s items (pending=%s)",
                len(entries),
                queue_depth,
            )
        except Exception as exc:  # pragma: no cover - hardware path
            self.logger.exception("Failed to push update to e-ink display: %s", exc)

    def _build_display_entries(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for record in records[: self.max_items]:
            enriched = dict(record)
            if record.get("status") == "completed":
                enriched["stdout_preview"] = extract_stdout_preview(record.get("log_path") or "")
            else:
                enriched["stdout_preview"] = ""
            entries.append(enriched)
        return entries
