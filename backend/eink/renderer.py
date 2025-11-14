"""Utility routines for converting queue status into an e-ink friendly image."""

from __future__ import annotations

import datetime as dt
import socket
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont


TITLE_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)

BODY_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


class StatusRenderer:
    """Create monochrome bitmaps summarising queue status."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self._title_font = self._load_font(size=84, candidates=TITLE_FONT_CANDIDATES)
        self._body_font = self._load_font(size=46, candidates=BODY_FONT_CANDIDATES)
        self._margin = 56
        self._text_x = self._margin
        self._detail_indent = "   "
        self._detail_line_count = 3
        available_width = self.width - (2 * self._margin)
        indent_width = self._measure_text(self._detail_indent)
        self._max_detail_width = max(60, available_width - int(indent_width))
        self._line_spacing = self._body_font.size + 6
        self._footer_font = self._load_font(size=40, candidates=BODY_FONT_CANDIDATES)
        self._footer_padding = self._footer_font.size + 24

    def render(
        self,
        entries: Sequence[Mapping[str, str]],
    ) -> Image.Image:
        """Return a greyscale PIL image containing queue metadata."""
        canvas = Image.new("L", (self.width, self.height), color=0xFF)
        draw = ImageDraw.Draw(canvas)
        y = self._margin
        content_bottom = max(self._margin + self._body_font.size, self.height - self._margin - self._footer_padding)

        title = "Agent Task Queue"
        draw.text((self._margin, y), title, font=self._title_font, fill=0x00)
        y += self._title_font.size + 30

        for idx, record in enumerate(entries, start=1):
            if y + self._body_font.size > content_bottom:
                break
            status = (record.get("status") or "unknown").lower()
            block_lines = self._format_entry(idx, record, status=status)
            for line_idx, line in enumerate(block_lines):
                draw.text((self._text_x, y), line, font=self._body_font, fill=0x00)
                y += self._line_spacing
            y += 10
            if y > content_bottom:
                break

        footer_left, footer_right = self._build_footer_labels()
        footer_y = self.height - self._margin - self._footer_font.size
        if footer_left:
            draw.text((self._margin, footer_y), footer_left, font=self._footer_font, fill=0x00)
        if footer_right:
            right_width = self._measure_text(footer_right, font=self._footer_font)
            right_x = max(self._margin, self.width - self._margin - right_width)
            draw.text((right_x, footer_y), footer_right, font=self._footer_font, fill=0x00)

        return canvas

    # ----------------------------------------------------------------- helpers
    def _format_entry(self, idx: int, record: Mapping[str, str], status: str) -> list[str]:
        status_label = {
            "queued": "PENDING",
            "running": "RUNNING",
            "completed": "COMPLETED",
            "failed": "FAILED",
        }.get(status, status.upper())
        created_at = self._parse_timestamp(record.get("created_at"))
        updated_at = self._parse_timestamp(record.get("updated_at"))
        runtime = self._format_duration(created_at, updated_at) if status in {"completed", "failed"} else None
        created_str = self._format_created_timestamp(created_at)
        runtime_str = runtime or "--:--"
        header = f"{idx:>2} | {status_label:<9} | {created_str:<16} | {runtime_str}"

        is_completed = status == "completed"
        if is_completed:
            detail_source = record.get("stdout_preview") or record.get("result_summary") or ""
            placeholder = "Stdout unavailable"
        else:
            detail_source = record.get("text", "")
            placeholder = "Prompt unavailable"
        detail_lines = self._wrap_detail_lines(detail_source, placeholder=placeholder)
        return [header, *detail_lines]

    def _wrap_detail_lines(self, text: str, *, placeholder: str) -> list[str]:
        normalized = " ".join((text or "").split()) or placeholder
        words = normalized.split()
        if not words:
            words = [placeholder]

        lines: list[str] = []
        current = ""
        idx = 0
        overflow = False
        max_width = self._max_detail_width
        max_lines = self._detail_line_count

        while idx < len(words):
            word = words[idx]
            candidate = f"{current} {word}".strip()
            candidate_width = self._measure_text(candidate) if candidate else 0
            if candidate and candidate_width <= max_width:
                current = candidate
                idx += 1
                continue

            if current:
                lines.append(current)
                current = ""
                if len(lines) >= max_lines:
                    overflow = True
                    break
                continue

            clipped = self._clip_to_width(word, max_width, ellipsis=True)
            lines.append(clipped)
            idx += 1
            if len(lines) >= max_lines:
                overflow = True
                break

        if not overflow and current:
            lines.append(current)

        if idx < len(words):
            overflow = True

        while len(lines) < max_lines:
            lines.append("")

        if lines:
            lines[-1] = self._clip_to_width(lines[-1], max_width, ellipsis=overflow)

        padded = [f"{self._detail_indent}{line}" if line else self._detail_indent for line in lines[:max_lines]]
        return padded

    def _clip_to_width(self, text: str, max_width: float, *, ellipsis: bool) -> str:
        if not text:
            return "…" if ellipsis else ""
        target = text
        suffix = "…" if ellipsis else ""
        while target and self._measure_text(f"{target}{suffix}") > max_width:
            target = target[:-1]
        if not target:
            return suffix or ""
        return f"{target}{suffix}"

    def _measure_text(self, text: str, font: ImageFont.ImageFont | ImageFont.FreeTypeFont | None = None) -> float:
        target_font = font or getattr(self, "_body_font", ImageFont.load_default())
        if hasattr(target_font, "getlength"):
            return target_font.getlength(text)
        return target_font.getsize(text)[0]

    def _parse_timestamp(self, value: str | None) -> dt.datetime | None:
        if not value:
            return None
        try:
            cleaned = value.replace("Z", "+00:00")
            return dt.datetime.fromisoformat(cleaned)
        except ValueError:
            return None

    def _format_created_timestamp(self, ts: dt.datetime | None) -> str:
        if not ts:
            return "--"
        return ts.astimezone(dt.timezone.utc).strftime("%d %b %H:%M")

    def _format_duration(self, start: dt.datetime | None, end: dt.datetime | None) -> str | None:
        if not start or not end:
            return None
        total_seconds = int((end - start).total_seconds())
        if total_seconds < 0:
            return None
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}:{seconds:02d}"

    def _load_font(
        self,
        size: int,
        candidates: tuple[str, ...] = TITLE_FONT_CANDIDATES,
    ) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        for path in candidates:
            font_path = Path(path)
            if font_path.exists():
                return ImageFont.truetype(str(font_path), size=size)
        return ImageFont.load_default()

    def _build_footer_labels(self) -> tuple[str, str]:
        ip_address = self._get_primary_ip() or "0.0.0.0"
        hostname = socket.gethostname() or "unknown"
        left = f"{ip_address} / {hostname}"
        timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        return left, timestamp

    def _get_primary_ip(self) -> str | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                # No packets are sent; we only use the socket to determine the bound interface.
                sock.connect(("8.8.8.8", 80))
                return sock.getsockname()[0]
        except OSError:
            try:
                return socket.gethostbyname(socket.gethostname())
            except OSError:
                return None
