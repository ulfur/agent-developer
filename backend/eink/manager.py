"""Background worker that renders queue status to the e-ink display."""

from __future__ import annotations

import datetime as dt
import logging
import queue
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Sequence, Tuple

from .it8591 import DU_MODE, DisplayUnavailable, IT8591Config, IT8591DisplayDriver
from .power import normalize_power_payload
from .renderer import StatusRenderer
from log_utils import extract_stdout_preview

SUBTITLE_REFRESH_INTERVAL = dt.timedelta(seconds=45)
QUEUE_WAIT_TIMEOUT = 0.25  # seconds; lower latency for overlays/updates


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
        power_monitor: Any | None = None,
        power_cache: Any | None = None,
        power_poll_interval_sec: float = 10.0,
        overlay_fast_mode: bool = False,
    ):
        super().__init__(daemon=True)
        self.store = store
        self.logger = logger
        self.enabled = enabled
        self.config = config
        self.max_items = max_items
        self.preferences = preferences
        self.human_tasks = human_tasks
        self.power_monitor = power_monitor
        self.power_cache = power_cache
        self._overlay: dict[str, Any] | None = None
        self._overlay_lock = threading.Lock()
        self._overlay_timer: threading.Timer | None = None
        self._overlay_fast_mode = overlay_fast_mode
        self._power_poll_interval = dt.timedelta(seconds=max(1.0, power_poll_interval_sec))
        self._next_power_poll: dt.datetime | None = None
        self._queue: "queue.Queue[tuple[str, tuple[str, ...] | None]]" = queue.Queue(maxsize=5)
        self._queue_wait_timeout = max(0.1, QUEUE_WAIT_TIMEOUT)
        self._stop = threading.Event()
        self._driver: IT8591DisplayDriver | None = None
        self._renderer: StatusRenderer | None = None
        self._last_success: dt.datetime | None = None
        self._init_failed_at: dt.datetime | None = None
        self._next_subtitle_refresh: dt.datetime | None = None
        self._power_refresh_signature: Tuple[Any, Any, Any] | None = None
        self._refresh_in_progress = False
        self._footer_refresh_interval = dt.timedelta(minutes=5)
        self._next_footer_refresh: dt.datetime | None = dt.datetime.utcnow()
        self._last_power_percent: float | None = None
        self._last_power_state: str | None = None
        self._last_power_refresh: dt.datetime | None = None
        self._power_change_threshold = 2.0
        self._power_refresh_cooldown = dt.timedelta(seconds=30)

    def request_refresh(self, reason: str = "", sections: tuple[str, ...] | None = None) -> None:
        """Queue a refresh request if the subsystem is enabled."""
        if not self.enabled:
            return
        if self._driver is None and not self._ensure_driver():
            return
        try:
            self._queue.put_nowait((reason or "update", sections))
        except queue.Full:
            self._flush_refresh_queue()
            try:
                self._queue.put_nowait((reason or "update", sections))
            except queue.Full:
                self.logger.debug("Display queue saturated; dropping refresh %s", reason)

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
                _, sections = self._queue.get(timeout=self._queue_wait_timeout)
            except queue.Empty:
                self._maybe_poll_power()
                self._maybe_refresh_footer_clock()
                continue
            if self._stop.is_set():
                break
            self._refresh_panel(sections=sections)
            self._maybe_poll_power()
            self._maybe_refresh_footer_clock()

    def stop(self) -> None:
        self._stop.set()
        self._display_shutdown_frame()
        self.request_refresh("shutdown")
        if self._driver:
            self._driver.close()
        closer = getattr(self.power_monitor, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception as exc:  # pragma: no cover - defensive cleanup
                self.logger.warning("Failed to close UPS monitor: %s", exc)

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
            self._schedule_next_subtitle_refresh()
            return True
        except DisplayUnavailable as exc:
            self.logger.warning("E-ink display unavailable: %s", exc)
            self._driver = None
            self._renderer = None
            self._init_failed_at = dt.datetime.utcnow()
            return False

    def _refresh_panel(self, sections: tuple[str, ...] | None = None) -> None:
        if not self._driver or not self._renderer:
            return
        self._refresh_in_progress = True
        try:
            overlay = self._active_overlay()
            invert = self._should_invert_display()
            entries: List[Dict[str, Any]] = []
            queue_depth = 0
            human_notifications = 0
            overlay_title = None
            if overlay:
                overlay_invert = overlay.get("invert")
                overlay_lines = overlay.get("lines") or []
                target_invert = invert if overlay_invert is None else bool(overlay_invert)
                overlay_title = str(overlay.get("title") or "Nightshift")
                image, bounds = self._renderer.render_overlay(
                    overlay_title,
                    overlay_lines,
                    invert=target_invert,
                )
                self.logger.info(
                    "Overlay bounds x=%s y=%s w=%s h=%s",
                    bounds[0],
                    bounds[1],
                    bounds[2],
                    bounds[3],
                )
                if self._overlay_fast_mode and hasattr(self._driver, "display_region"):
                    self._driver.display_region(image, bounds, mode=DU_MODE)
                else:
                    self._driver.display_image(image)
            else:
                snapshot = self.store.list_prompts()
                human_records, human_summary = self._collect_human_tasks()
                entries = self._build_display_entries(snapshot.get("items", []), human_records)
                queue_depth = self.store.pending_count()
                human_notifications = self._calculate_human_notifications(human_summary)
                power_status = self._read_power_status_payload()
                if sections:
                    image, section_images = self._renderer.render_with_sections(
                        entries,
                        invert=invert,
                        pending_count=queue_depth,
                        human_notification_count=human_notifications,
                        power_status=power_status,
                    )
                    for name in sections:
                        region = section_images.get(name)
                        if not region:
                            continue
                        region_image, bounds = region
                        try:
                            self._driver.display_region(region_image, bounds, mode=DU_MODE)
                        except Exception:
                            self._driver.display_image(image)
                            break
                else:
                    image = self._renderer.render(
                        entries,
                        invert=invert,
                        pending_count=queue_depth,
                        human_notification_count=human_notifications,
                        power_status=power_status,
                    )
                    self._driver.display_image(image)
            self._last_success = dt.datetime.utcnow()
            if overlay_title:
                mode_label = "fast" if self._overlay_fast_mode else "full"
                self.logger.info("E-ink overlay '%s' rendered (%s)", overlay_title, mode_label)
            elif sections:
                self.logger.info("E-ink sections refreshed: %s", ",".join(sections))
            else:
                self.logger.info(
                    "E-ink display updated with %s items (pending=%s, human_notifications=%s)",
                    len(entries),
                    queue_depth,
                    human_notifications,
                )
        except Exception as exc:  # pragma: no cover - hardware path
            self.logger.exception("Failed to push update to e-ink display: %s", exc)
        finally:
            self._refresh_in_progress = False
            self._schedule_next_subtitle_refresh()

    def _display_shutdown_frame(self) -> None:
        """Render a final shutdown frame before powering the display off."""
        if not self.enabled:
            return
        if not self._driver or not self._renderer:
            if not self._ensure_driver():
                return
        if not self._driver or not self._renderer:
            return
        try:
            image = self._renderer.render_shutdown_frame()
        except Exception as exc:  # pragma: no cover - hardware path
            self.logger.warning("Unable to render shutdown frame: %s", exc)
            return
        try:
            self._driver.display_image(image)
            self.logger.info("E-ink display updated with shutdown frame")
        except Exception as exc:  # pragma: no cover - hardware path
            self.logger.warning("Failed to push shutdown frame to e-ink display: %s", exc)

    def _schedule_next_subtitle_refresh(self) -> None:
        self._next_subtitle_refresh = None

    def _maybe_rotate_subtitle(self) -> None:
        return

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

    def _read_power_status_payload(self) -> Dict[str, Any] | None:
        if not self.power_monitor:
            return None
        reader = getattr(self.power_monitor, "read_status", None)
        if not callable(reader):
            return None
        try:
            snapshot = reader()
        except Exception as exc:  # pragma: no cover - hardware path
            self.logger.warning("Failed to read UPS telemetry: %s", exc)
            return None
        payload = normalize_power_payload(snapshot)
        if payload:
            self._publish_power_status(payload)
        return payload or None

    def _publish_power_status(self, payload: Dict[str, Any]) -> None:
        if not payload:
            return
        cache = self.power_cache
        if not cache:
            return
        updater = getattr(cache, "update", None)
        if not callable(updater):
            return
        try:
            updater(payload)
        except Exception as exc:  # pragma: no cover - optional publishing
            self.logger.debug("Failed to broadcast UPS telemetry snapshot: %s", exc)
            return
        signature = (
            payload.get("ac_power"),
            payload.get("state"),
            payload.get("low_battery"),
        )
        if signature != self._power_refresh_signature:
            self._power_refresh_signature = signature
            if not self._refresh_in_progress:
                self.logger.debug("Power state changed; scheduling aux display refresh")
                self.request_refresh("power-change", sections=("header_right",))
        self._maybe_refresh_power_section(payload)

    def _maybe_poll_power(self) -> None:
        if not self.power_monitor:
            return
        now = dt.datetime.utcnow()
        if self._next_power_poll and now < self._next_power_poll:
            return
        self._next_power_poll = now + self._power_poll_interval
        self._read_power_status_payload()

    def _maybe_refresh_power_section(self, payload: Dict[str, Any]) -> None:
        percent = payload.get("percent")
        state = payload.get("state")
        try:
            percent_value = float(percent) if percent is not None else None
        except (TypeError, ValueError):
            percent_value = None
        now = dt.datetime.utcnow()
        changed = False
        if percent_value is not None and self._last_power_percent is not None:
            if abs(percent_value - self._last_power_percent) >= self._power_change_threshold:
                changed = True
        elif percent_value is not None and self._last_power_percent is None:
            changed = True
        if state != self._last_power_state:
            changed = True
        if not changed:
            return
        if self._last_power_refresh and (now - self._last_power_refresh) < self._power_refresh_cooldown:
            self._last_power_percent = percent_value
            self._last_power_state = state
            return
        self._last_power_percent = percent_value
        self._last_power_state = state
        self._last_power_refresh = now
        self.request_refresh("power-change", sections=("header_right",))

    def _maybe_refresh_footer_clock(self) -> None:
        now = dt.datetime.utcnow()
        if self._next_footer_refresh and now < self._next_footer_refresh:
            return
        self._next_footer_refresh = now + self._footer_refresh_interval
        self.request_refresh("footer-clock", sections=("footer_right",))

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

    # -------------------------------------------------------------- overlays
    def show_overlay(
        self,
        title: str,
        lines: Sequence[str],
        *,
        duration_sec: float | None = None,
        invert: bool | None = None,
    ) -> Dict[str, Any]:
        normalized_title = (title or "Nightshift").strip() or "Nightshift"
        normalized_lines = [str(line or "").strip() for line in lines]
        if not any(normalized_lines):
            normalized_lines = [""]
        expires_at: dt.datetime | None = None
        if duration_sec and duration_sec > 0:
            expires_at = dt.datetime.utcnow() + dt.timedelta(seconds=float(duration_sec))
        overlay_payload = {
            "title": normalized_title,
            "lines": normalized_lines,
            "invert": invert,
            "expires_at": expires_at,
        }
        with self._overlay_lock:
            self._overlay = overlay_payload
        self._schedule_overlay_timer(duration_sec)
        self.logger.info(
            "Overlay scheduled title=%s duration=%s", normalized_title, duration_sec or "indefinite"
        )
        self._flush_refresh_queue()
        self.request_refresh("overlay-set")
        return {
            "title": normalized_title,
            "lines": normalized_lines,
            "invert": invert,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }

    def clear_overlay(self) -> None:
        cleared = False
        with self._overlay_lock:
            if self._overlay is not None:
                self._overlay = None
                cleared = True
        self._cancel_overlay_timer()
        if cleared:
            self.logger.info("Overlay cleared; restoring queue display")
            self.request_refresh("overlay-cleared", sections=("body",))

    def _active_overlay(self) -> Dict[str, Any] | None:
        with self._overlay_lock:
            overlay = self._overlay
            if not overlay:
                return None
            expires_at = overlay.get("expires_at")
            if isinstance(expires_at, dt.datetime) and dt.datetime.utcnow() >= expires_at:
                self._overlay = None
                return None
            return dict(overlay)

    def _schedule_overlay_timer(self, duration_sec: float | None) -> None:
        self._cancel_overlay_timer()
        if not duration_sec or duration_sec <= 0:
            return
        timer = threading.Timer(float(duration_sec), self._handle_overlay_timeout)
        timer.daemon = True
        self._overlay_timer = timer
        timer.start()

    def _cancel_overlay_timer(self) -> None:
        timer = self._overlay_timer
        if timer:
            timer.cancel()
        self._overlay_timer = None

    def _handle_overlay_timeout(self) -> None:
        cleared = False
        with self._overlay_lock:
            overlay = self._overlay
            if overlay is None:
                cleared = False
            else:
                expires_at = overlay.get("expires_at")
                if isinstance(expires_at, dt.datetime) and dt.datetime.utcnow() >= expires_at:
                    self._overlay = None
                    cleared = True
        self._overlay_timer = None
        if cleared:
            self.logger.debug("Overlay expired; refreshing display")
            self.request_refresh("overlay-expired", sections=("body",))

    def _flush_refresh_queue(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            return
