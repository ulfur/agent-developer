"""Utility routines for converting queue status into an e-ink friendly image."""

from __future__ import annotations

import datetime as dt
import json
import random
import socket
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps
import textwrap


TITLE_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)

SUBTITLE_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)

BODY_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)

HEADER_SUBTITLE_MESSAGES: tuple[str, ...] = (
    "all day every day",
    "the code writes itself",
    "you think, code happens",
    "commit, push, repeat",
    "deploys before sunrise",
    "sleep is for staging",
    "bugs fear this place",
    "runtime errors take PTO",
)


def pick_header_subtitle(previous: str | None = None) -> str:
    """Return a random subtitle that differs from the previous value when possible."""
    cleaned_previous = (previous or "").strip()
    available = [message for message in HEADER_SUBTITLE_MESSAGES if message != cleaned_previous]
    if not available:
        return cleaned_previous or HEADER_SUBTITLE_MESSAGES[0]
    return random.choice(available)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_FONT_DIR = PROJECT_ROOT / "backend" / "assets" / "fonts"
ICON_FONT_CANDIDATES = (
    str(ASSET_FONT_DIR / "materialdesignicons-webfont.ttf"),
    str(ASSET_FONT_DIR / "MaterialIcons-Regular.ttf"),
    "/usr/share/fonts/truetype/material-design-icons/MaterialIcons-Regular.ttf",
)
ICON_CODEPOINTS_PATH = ASSET_FONT_DIR / "mdi-battery-codepoints.json"
HEADER_ICON_VARIANTS = {
    "dark": PROJECT_ROOT / "frontend" / "nightshift-header-dark.png",
    "light": PROJECT_ROOT / "frontend" / "nightshift-header-light.png",
}
# Apply another 25% bump on top of the previous 50% increase (~87.5% over baseline).
HEADER_ICON_ENLARGE_FACTOR = 1.5 * 1.25
HEADER_ICON_BASE_SCALE = 1.05  # preserves the legacy scaling before enlargement


class StatusRenderer:
    """Create monochrome bitmaps summarising queue status."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self._title_font = self._load_font(size=84, candidates=TITLE_FONT_CANDIDATES)
        subtitle_size = max(32, int(self._title_font.size * 0.45))
        self._subtitle_font = self._load_font(size=subtitle_size, candidates=SUBTITLE_FONT_CANDIDATES)
        self._subtitle_text = pick_header_subtitle()
        self._subtitle_gap = max(8, self._subtitle_font.size // 4)
        self._body_font = self._load_font(size=48, candidates=BODY_FONT_CANDIDATES)
        self._icon_font = self._load_icon_font(size=max(64, int(self._body_font.size * 1.75)))
        self._icon_glyphs = self._load_icon_codepoints()
        logo_size = int(self._title_font.size * HEADER_ICON_BASE_SCALE * HEADER_ICON_ENLARGE_FACTOR)
        self._header_logos = self._load_header_logos(logo_size)
        self._logo_text_gap = max(12, logo_size // 6)
        self._margin = 46
        self._top_margin = max(0, self._margin - 30)
        self._text_x = self._margin
        self._detail_indent = "   "
        self._detail_line_count = 3
        available_width = self.width - (2 * self._margin)
        indent_width = self._measure_text(self._detail_indent)
        self._max_detail_width = max(60, available_width - int(indent_width))
        self._line_spacing = self._body_font.size + 6
        self._footer_font = self._load_font(size=44, candidates=BODY_FONT_CANDIDATES)
        self._footer_margin = max(18, self._margin // 3)

    def render(
        self,
        entries: Sequence[Mapping[str, str]],
        *,
        invert: bool = False,
        pending_count: int | None = None,
        human_notification_count: int | None = None,
        power_status: Mapping[str, Any] | None = None,
    ) -> Image.Image:
        """Return a greyscale PIL image containing queue metadata."""
        canvas = Image.new("L", (self.width, self.height), color=0xFF)
        draw = ImageDraw.Draw(canvas)
        y = self._top_margin
        footer_space = self._footer_margin + self._footer_font.size
        footer_block_top = self.height - footer_space
        content_bottom = max(self._top_margin + self._body_font.size, footer_block_top - 10)

        self._subtitle_text = pick_header_subtitle(self._subtitle_text)

        y = self._draw_header(
            canvas,
            draw,
            y,
            invert,
            pending_count=pending_count,
            human_notification_count=human_notification_count,
            power_status=power_status,
        )

        human_count = 0
        agent_count = 0
        divider_drawn = False
        for record in entries:
            if y + self._body_font.size > content_bottom:
                break
            status = (record.get("status") or "unknown").lower()
            entry_type = str(record.get("entry_type") or "agent").lower()
            is_human = entry_type == "human"
            if is_human:
                human_count += 1
                display_idx = human_count
            else:
                if human_count > 0 and not divider_drawn:
                    draw.line(
                        (self._margin, y, self.width - self._margin, y),
                        fill=0x00,
                        width=2,
                    )
                    y += 12
                    divider_drawn = True
                agent_count += 1
                display_idx = agent_count
            block_lines = self._format_entry(display_idx, record, status=status)
            for line_idx, line in enumerate(block_lines):
                draw.text((self._text_x, y), line, font=self._body_font, fill=0x00)
                y += self._line_spacing
            y += 10
            if y > content_bottom:
                break

        footer_left, footer_right = self._build_footer_labels()
        footer_y = self.height - self._footer_margin - self._footer_font.size
        if footer_left:
            draw.text((self._margin, footer_y), footer_left, font=self._footer_font, fill=0x00)
        if footer_right:
            right_width = self._measure_text(footer_right, font=self._footer_font)
            right_x = max(self._margin, self.width - self._margin - right_width)
            draw.text((right_x, footer_y), footer_right, font=self._footer_font, fill=0x00)

        if invert:
            canvas = ImageOps.invert(canvas)

        return canvas

    def render_with_sections(
        self,
        entries: Sequence[Mapping[str, str]],
        *,
        invert: bool = False,
        pending_count: int | None = None,
        human_notification_count: int | None = None,
        power_status: Mapping[str, Any] | None = None,
    ) -> tuple[Image.Image, dict[str, tuple[Image.Image, tuple[int, int, int, int]]]]:
        canvas = self.render(
            entries,
            invert=invert,
            pending_count=pending_count,
            human_notification_count=human_notification_count,
            power_status=power_status,
        )
        sections = self._extract_section_images(canvas)
        return canvas, sections

    def render_overlay(
        self,
        title: str,
        lines: Sequence[str],
        invert: bool = False,
    ) -> tuple[Image.Image, tuple[int, int, int, int]]:
        canvas = Image.new("L", (self.width, self.height), color=0xFF)
        draw = ImageDraw.Draw(canvas)

        window_height = max(int(self.height * 0.4), self._title_font.size * 3)
        window_width = max(int(self.width * 0.4), self._text_x * 2)
        window_width = min(window_width, self.width - 2 * self._margin)
        window_height = min(window_height, self.height - 2 * self._margin)
        window_x = max(self._margin, (self.width - window_width) // 2)
        window_y = max(self._margin, (self.height - window_height) // 2)
        window_x, window_width = self._align_horizontal_bounds(window_x, window_width)
        bounds = (window_x, window_y, window_width, window_height)

        border_radius = 20
        border_rect = [window_x, window_y, window_x + window_width, window_y + window_height]
        draw.rounded_rectangle(border_rect, radius=border_radius, outline=0x00, width=4, fill=0xFF)

        inner_margin = 24
        text_x = window_x + inner_margin
        text_y = window_y + inner_margin
        text_width = window_width - 2 * inner_margin

        draw.text((text_x, text_y), title, font=self._title_font, fill=0x00)
        text_y += self._title_font.size + 16

        wrapped_lines: list[str] = []
        for line in lines:
            wrapped_lines.extend(self._wrap_text(line, self._body_font, text_width))
        for line in wrapped_lines:
            if text_y + self._body_font.size > window_y + window_height - inner_margin:
                break
            draw.text((text_x, text_y), line, font=self._body_font, fill=0x00)
            text_y += self._body_font.size + 10

        if invert:
            canvas = ImageOps.invert(canvas)
        return canvas, bounds

    def _align_horizontal_bounds(self, x: int, width: int) -> tuple[int, int]:
        start = max(self._margin, x)
        end = min(self.width - self._margin, start + width)
        aligned_start = (start // 4) * 4
        aligned_end = ((end + 3) // 4) * 4
        aligned_start = max(self._margin, aligned_start)
        aligned_end = min(self.width - self._margin, aligned_end)
        if aligned_end <= aligned_start:
            aligned_end = min(self.width - self._margin, aligned_start + 4)
        # keep the window centred by shifting equally when possible
        desired = width
        current = aligned_end - aligned_start
        if current < desired:
            deficit = desired - current
            shift_left = min(deficit // 2, aligned_start - self._margin)
            aligned_start -= shift_left
            aligned_end = min(self.width - self._margin, aligned_start + desired)
            remainder = (aligned_end - aligned_start) % 4
            if remainder:
                aligned_end = min(self.width - self._margin, aligned_end + (4 - remainder))
        return aligned_start, aligned_end - aligned_start

    def render_shutdown_frame(self) -> Image.Image:
        """Return an all-black frame with the Nightshift mark centered."""
        canvas = Image.new("L", (self.width, self.height), color=0x00)
        logo = self._select_shutdown_logo()
        if logo:
            logo_bitmap = logo.copy()
            max_side = int(min(self.width, self.height) * 0.65)
            if max_side > 0:
                logo_bitmap.thumbnail((max_side, max_side), Image.LANCZOS)
            x = max(0, (self.width - logo_bitmap.width) // 2)
            y = max(0, (self.height - logo_bitmap.height) // 2)
            canvas.paste(logo_bitmap, (x, y))
        return canvas

    def _extract_section_images(
        self, canvas: Image.Image
    ) -> dict[str, tuple[Image.Image, tuple[int, int, int, int]]]:
        boxes = self._compute_section_boxes()
        crops: dict[str, tuple[Image.Image, tuple[int, int, int, int]]] = {}
        for name, box in boxes.items():
            x, y, w, h = box
            if w <= 0 or h <= 0:
                continue
            region = canvas.crop((x, y, x + w, y + h))
            crops[name] = (region, box)
        return crops

    def _compute_section_boxes(self) -> dict[str, tuple[int, int, int, int]]:
        boxes: dict[str, tuple[int, int, int, int]] = {}
        content_width = self.width - (2 * self._margin)
        header_height = self._align_span(self._title_font.size + max(self._subtitle_font.size, 32) + 24)
        footer_height = self._align_span(self._footer_font.size + 24)
        header_left_width = self._align_span(int(content_width * 0.6))
        header_right_width = self._align_span(content_width - header_left_width)
        header_top = self._top_margin
        header_left_box = self._align_box(self._margin, header_top, header_left_width, header_height)
        header_right_box = self._align_box(
            self._margin + header_left_width,
            header_top,
            header_right_width,
            header_height,
        )
        body_top = header_top + header_height + 12
        footer_top = self.height - footer_height - self._footer_margin
        body_height = max(4, footer_top - body_top - 12)
        body_box = self._align_box(self._margin, body_top, content_width, body_height)
        footer_left_box = self._align_box(self._margin, footer_top, header_left_width, footer_height)
        footer_right_box = self._align_box(
            self._margin + header_left_width,
            footer_top,
            header_right_width,
            footer_height,
        )
        boxes["header_left"] = header_left_box
        boxes["header_right"] = header_right_box
        boxes["body"] = body_box
        boxes["footer_left"] = footer_left_box
        boxes["footer_right"] = footer_right_box
        return boxes

    def _align_span(self, value: int) -> int:
        if value <= 0:
            return 4
        remainder = value % 4
        if remainder == 0:
            return value
        return value + (4 - remainder)

    def _align_box(self, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
        x = max(0, x - (x % 4))
        y = max(0, y - (y % 4))
        w = max(4, w)
        h = max(4, h)
        x2 = min(self.width, x + w)
        y2 = min(self.height, y + h)
        w = max(4, x2 - x)
        h = max(4, y2 - y)
        if w % 4:
            w -= w % 4
        if h % 4:
            h -= h % 4
        return x, y, w, h

    # ----------------------------------------------------------------- helpers
    def _format_entry(self, idx: int, record: Mapping[str, Any], status: str) -> list[str]:
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
        project_label = self._extract_project_label(record)
        if project_label:
            header = f"{header} | {project_label}"

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

    def _wrap_text(self, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
        if not text:
            return [""]
        result: list[str] = []
        for paragraph in text.splitlines() or [""]:
            if not paragraph.strip():
                result.append("")
                continue
            words = paragraph.split()
            current = ""
            for word in words:
                candidate = (current + " " + word).strip()
                if candidate and self._measure_text(candidate, font=font) <= max_width:
                    current = candidate
                else:
                    if current:
                        result.append(current)
                    current = word
            if current:
                result.append(current)
        return result or [""]

    def _extract_project_label(self, record: Mapping[str, Any]) -> str:
        project = record.get("project")
        name = ""
        if isinstance(project, Mapping):
            name = (project.get("name") or project.get("id") or "").strip()
        if not name:
            name = (record.get("project_id") or "").strip()
        return name

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

    def _draw_header(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        y: int,
        invert: bool,
        *,
        pending_count: int | None,
        human_notification_count: int | None,
        power_status: Mapping[str, Any] | None = None,
    ) -> int:
        title = "Nightshift"
        subtitle = (self._subtitle_text or "").strip()
        logo = self._select_header_logo(invert)
        subtitle_block = bool(subtitle)
        subtitle_font = self._subtitle_font if subtitle_block else None
        subtitle_height = subtitle_font.size if subtitle_font else 0
        title_block_height = self._title_font.size + (self._subtitle_gap if subtitle_block else 0) + subtitle_height
        power_block = self._build_power_display(power_status)
        stats_lines = [] if power_block else self._format_header_stats_lines(
            pending_count,
            human_notification_count,
        )
        stats_font = self._body_font
        stats_line_gap = max(6, stats_font.size // 3)
        stats_block_height = 0
        stats_width = 0
        glyph_width = 0
        glyph_gap = 0
        glyph_font = None
        text_lines: list[str] = []
        if power_block:
            glyph_text = power_block.get("glyph") or ""
            glyph_font = power_block.get("glyph_font") or stats_font
            text_lines = [
                line
                for line in (
                    power_block.get("primary"),
                    power_block.get("secondary"),
                )
                if line
            ]
            glyph_width = self._measure_text(glyph_text, font=glyph_font) if glyph_text else 0
            glyph_gap = max(12, stats_font.size // 2) if glyph_text and text_lines else 0
            text_width = max((self._measure_text(line, font=stats_font) for line in text_lines), default=0)
            stats_width = glyph_width + glyph_gap + text_width
            text_block_height = (
                (stats_font.size * len(text_lines))
                + (stats_line_gap * (len(text_lines) - 1) if len(text_lines) > 1 else 0)
                if text_lines
                else 0
            )
            glyph_height = glyph_font.size if glyph_text else 0
            stats_block_height = max(glyph_height, text_block_height)
        elif stats_lines:
            stats_block_height = (stats_font.size * len(stats_lines)) + (
                stats_line_gap * (len(stats_lines) - 1) if len(stats_lines) > 1 else 0
            )
            stats_width = max(int(self._measure_text(line, font=stats_font)) for line in stats_lines)
        logo_height = logo.height if logo else 0
        header_height = max(title_block_height, logo_height, stats_block_height)
        text_x = self._margin + (logo.width + self._logo_text_gap if logo else 0)
        text_y = y + max(0, (header_height - title_block_height) // 2)
        draw.text((text_x, text_y), title, font=self._title_font, fill=0x00)
        if subtitle_block and subtitle_font:
            aligned_subtitle_x = text_x
            subtitle_y = text_y + self._title_font.size + self._subtitle_gap
            draw.text((aligned_subtitle_x, subtitle_y), subtitle, font=subtitle_font, fill=0x00)
        if logo:
            logo_y = y + max(0, (header_height - logo.height) // 2)
            canvas.paste(logo, (self._margin, logo_y))
        if power_block:
            stats_x = max(self._margin, self.width - self._margin - stats_width)
            stats_y = y + max(0, (header_height - stats_block_height) // 2)
            glyph_text = power_block.get("glyph") or ""
            glyph_font = glyph_font or stats_font
            text_x = stats_x
            text_y = stats_y + max(
                0,
                (
                    stats_block_height
                    - (
                        (stats_font.size * len(text_lines))
                        + (stats_line_gap * (len(text_lines) - 1) if len(text_lines) > 1 else 0)
                    )
                )
                // 2,
            ) if text_lines else stats_y
            current_y = text_y
            text_width = 0
            for line in text_lines:
                draw.text((text_x, current_y), line, font=stats_font, fill=0x00)
                text_width = max(text_width, self._measure_text(line, font=stats_font))
                current_y += stats_font.size + stats_line_gap
            icon_x = text_x + (text_width if text_lines else 0) + (glyph_gap if glyph_text else 0)
            if glyph_text:
                icon_y = stats_y + max(0, (stats_block_height - glyph_font.size) // 2)
                draw.text((icon_x, icon_y), glyph_text, font=glyph_font, fill=0x00)
        elif stats_lines:
            stats_x = max(self._margin, self.width - self._margin - stats_width)
            stats_y = y + max(0, (header_height - stats_block_height) // 2)
            for idx, line in enumerate(stats_lines):
                line_y = stats_y + idx * (stats_font.size + stats_line_gap)
                draw.text((stats_x, line_y), line, font=stats_font, fill=0x00)
        return y + header_height + 30

    def _build_power_display(self, power_status: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not power_status:
            return None
        percentage = self._coerce_float(power_status.get("percentage"))
        clamped = None if percentage is None else max(0.0, min(100.0, percentage))
        if clamped is None and not power_status.get("state"):
            return None
        ac_power = power_status.get("ac_power")
        state_value = str(power_status.get("state") or "").lower()
        low_battery = bool(power_status.get("low_battery"))
        charging = state_value == "charging" or ac_power is True
        discharging = state_value == "battery" or ac_power is False
        glyph_text = ""
        glyph_font: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None
        icon_name = self._select_battery_icon_name(clamped, charging, discharging, low_battery)
        if icon_name and self._icon_font:
            icon_char = self._get_icon_glyph(icon_name)
            if icon_char:
                glyph_text = icon_char
                glyph_font = self._icon_font
        if not glyph_text:
            glyph_text = self._render_ascii_battery_bar(clamped, charging, discharging, low_battery)
            glyph_font = self._body_font
        primary = f"{clamped:.0f}%" if clamped is not None else ""
        if not any([glyph_text, primary]):
            return None
        return {
            "glyph": glyph_text,
            "glyph_font": glyph_font or self._body_font,
            "primary": primary,
            "secondary": "",
        }

    def _format_header_stats_lines(
        self,
        pending_count: int | None,
        human_notification_count: int | None,
    ) -> list[str]:
        normalized_pending = self._normalize_count(pending_count)
        normalized_human = self._normalize_count(human_notification_count)
        agent_label = normalized_pending if normalized_pending is not None else "--"
        human_label = normalized_human if normalized_human is not None else "--"
        return [
            f"Agent tasks: {agent_label}",
            f"Human tasks: {human_label}",
        ]

    def _render_ascii_battery_bar(
        self,
        percentage: float | None,
        charging: bool,
        discharging: bool,
        low_battery: bool,
    ) -> str:
        segments = 10
        if percentage is None:
            inner = "." * segments
        else:
            clamped = max(0.0, min(100.0, percentage))
            filled = int(round(clamped / 100 * segments))
            inner = "#" * filled + "-" * (segments - filled)
        indicator = ""
        if charging:
            indicator = "↑"
        elif low_battery:
            indicator = "!"
        elif discharging:
            indicator = "↓"
        return f"[{inner}]{indicator}"

    def _describe_power_state(self, power_status: Mapping[str, Any]) -> str | None:
        state = str(power_status.get("state") or "").lower()
        ac_power = power_status.get("ac_power")
        if state == "charging":
            return "Charging"
        if state == "charged":
            return "On AC"
        if state == "battery":
            return "On battery"
        if state == "ac":
            return "On AC"
        if isinstance(ac_power, bool):
            return "On AC" if ac_power else "On battery"
        return None

    def _select_battery_icon_name(
        self,
        percentage: float | None,
        charging: bool,
        discharging: bool,
        low_battery: bool,
    ) -> str | None:
        if percentage is None:
            return "battery_charging-outline" if charging else "battery-unknown"
        if percentage < 5:
            return "battery-alert-variant-outline"
        if charging:
            bucket = 100 if percentage >= 95 else max(10, min(90, int(percentage // 10) * 10))
            return "battery-charging-100" if bucket == 100 else f"battery-charging-{bucket}"
        if low_battery and percentage < 10:
            return "battery-alert"
        bucket = 100 if percentage >= 95 else max(10, min(90, int(percentage // 10) * 10))
        if bucket == 100:
            return "battery"
        return f"battery-{bucket}"

    def _get_icon_glyph(self, name: str | None) -> str | None:
        if not name:
            return None
        return self._icon_glyphs.get(name)

    def _coerce_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _normalize_count(self, value: int | None) -> int | None:
        if value is None:
            return None
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return None
        return max(0, numeric)

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

    def _load_icon_font(self, size: int) -> ImageFont.FreeTypeFont | None:
        for path in ICON_FONT_CANDIDATES:
            font_path = Path(path)
            if font_path.exists():
                try:
                    return ImageFont.truetype(str(font_path), size=size)
                except OSError:
                    continue
        return None

    def _load_icon_codepoints(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        path = ICON_CODEPOINTS_PATH
        if not path.exists():
            return mapping
        try:
            if path.suffix == ".json":
                data = json.loads(path.read_text(encoding="utf-8"))
                for name, code in data.items():
                    try:
                        mapping[name] = chr(int(code, 16))
                    except ValueError:
                        continue
                return mapping
            payload = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        for line in payload.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2:
                continue
            name, code = parts
            try:
                mapping[name] = chr(int(code, 16))
            except ValueError:
                continue
        return mapping

    def _build_footer_labels(self) -> tuple[str, str]:
        ip_address = self._get_primary_ip() or "0.0.0.0"
        hostname = socket.gethostname() or "unknown"
        suffix = ".local"
        normalized_host = hostname if hostname.endswith(suffix) else f"{hostname}{suffix}"
        left = f"{ip_address} / {normalized_host}"
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

    def _select_header_logo(self, invert: bool) -> Image.Image | None:
        """Return the grayscale header logo prepared for the target theme."""
        logos = getattr(self, "_header_logos", {})
        if not logos:
            return None
        primary = "dark" if invert else "light"
        secondary = "light" if primary == "dark" else "dark"
        candidate = logos.get(primary) or logos.get(secondary) or logos.get("fallback")
        if not candidate:
            return None
        if invert:
            return ImageOps.invert(candidate)
        return candidate

    def _select_shutdown_logo(self) -> Image.Image | None:
        """Return a bright logo variant suitable for a black fullscreen wipe."""
        logos = getattr(self, "_header_logos", {})
        if not logos:
            return None
        for key in ("light", "fallback", "dark"):
            candidate = logos.get(key)
            if candidate:
                return candidate
        return None

    def _load_header_logos(self, base_size: int) -> dict[str, Image.Image]:
        """Load available header assets plus a fallback mark."""
        logos: dict[str, Image.Image] = {}
        for variant, path in HEADER_ICON_VARIANTS.items():
            logo = self._load_header_png(path, base_size)
            if logo:
                logos[variant] = logo
        if not logos:
            logos["fallback"] = self._render_fallback_logo(base_size)
        return logos

    def _load_header_png(self, path: Path, base_size: int) -> Image.Image | None:
        """Convert a Nightshift header PNG into a grayscale bitmap for e-ink."""
        if not path.exists():
            return None
        try:
            with Image.open(path) as source:
                logo = source.convert("RGBA")
        except OSError:
            return None
        if logo.size != (base_size, base_size):
            logo = ImageOps.fit(logo, (base_size, base_size), method=Image.LANCZOS)
        grayscale = ImageOps.autocontrast(ImageOps.grayscale(logo.convert("RGB")))
        return grayscale

    def _render_fallback_logo(self, base_size: int) -> Image.Image:
        """Render the simplified fallback mark described in docs/nightshift-logo-spec.md."""

        size = max(48, base_size)
        scale = size / 36.0

        def px(value: float) -> int:
            return int(round(value * scale))

        def pt(x: float, y: float) -> tuple[int, int]:
            return (px(x), px(y))

        tile = Image.new("L", (size, size), color=0xFF)
        draw = ImageDraw.Draw(tile)

        circle_center = pt(18, 18)
        circle_radius = px(14)
        circle_color = 0x2C  # Approximated grayscale of #142d55 for e-ink
        draw.ellipse(
            (
                circle_center[0] - circle_radius,
                circle_center[1] - circle_radius,
                circle_center[0] + circle_radius,
                circle_center[1] + circle_radius,
            ),
            fill=circle_color,
        )

        stroke_width = max(2, px(3))
        stroke_color = 0xF2
        n_path = [
            pt(12, 26),
            pt(12, 10),
            pt(18, 26),
            pt(24, 10),
            pt(24, 26),
        ]
        draw.line(n_path, fill=stroke_color, width=stroke_width, joint="curve")

        cap_radius = max(1, stroke_width // 2)
        for point in n_path:
            draw.ellipse(
                (
                    point[0] - cap_radius,
                    point[1] - cap_radius,
                    point[0] + cap_radius,
                    point[1] + cap_radius,
                ),
                fill=stroke_color,
            )

        moon_center = pt(12, 8)
        moon_radius = max(2, px(3))
        moon_color = 0xCC  # Approximation of the golden crescent
        draw.ellipse(
            (
                moon_center[0] - moon_radius,
                moon_center[1] - moon_radius,
                moon_center[0] + moon_radius,
                moon_center[1] + moon_radius,
            ),
            fill=moon_color,
        )

        return tile
