"""Utility routines for converting queue status into an e-ink friendly image."""

from __future__ import annotations

import datetime as dt
import random
import socket
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps


TITLE_FONT_CANDIDATES = (
    # Prefer lighter weights so the header reads softer on the e-ink panel.
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)

SUBTITLE_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
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
        self._body_font = self._load_font(size=46, candidates=BODY_FONT_CANDIDATES)
        logo_size = int(self._title_font.size * HEADER_ICON_BASE_SCALE * HEADER_ICON_ENLARGE_FACTOR)
        self._header_logos = self._load_header_logos(logo_size)
        self._logo_text_gap = max(12, logo_size // 6)
        self._margin = 56
        self._text_x = self._margin
        self._detail_indent = "   "
        self._detail_line_count = 3
        available_width = self.width - (2 * self._margin)
        indent_width = self._measure_text(self._detail_indent)
        self._max_detail_width = max(60, available_width - int(indent_width))
        self._line_spacing = self._body_font.size + 6
        self._footer_font = self._load_font(size=40, candidates=BODY_FONT_CANDIDATES)
        self._footer_margin = max(18, self._margin // 3)

    def render(
        self,
        entries: Sequence[Mapping[str, str]],
        *,
        invert: bool = False,
        pending_count: int | None = None,
        human_notification_count: int | None = None,
    ) -> Image.Image:
        """Return a greyscale PIL image containing queue metadata."""
        canvas = Image.new("L", (self.width, self.height), color=0xFF)
        draw = ImageDraw.Draw(canvas)
        y = self._margin
        footer_space = self._footer_margin + self._footer_font.size
        footer_block_top = self.height - footer_space
        content_bottom = max(self._margin + self._body_font.size, footer_block_top - 10)

        self._subtitle_text = pick_header_subtitle(self._subtitle_text)

        y = self._draw_header(
            canvas,
            draw,
            y,
            invert,
            pending_count=pending_count,
            human_notification_count=human_notification_count,
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
    ) -> int:
        title = "Nightshift"
        subtitle = (self._subtitle_text or "").strip()
        logo = self._select_header_logo(invert)
        subtitle_block = bool(subtitle)
        subtitle_font = self._subtitle_font if subtitle_block else None
        subtitle_height = subtitle_font.size if subtitle_font else 0
        title_block_height = self._title_font.size + (self._subtitle_gap if subtitle_block else 0) + subtitle_height
        stats_lines = self._format_header_stats_lines(pending_count, human_notification_count)
        stats_font = self._body_font
        stats_line_gap = max(6, stats_font.size // 3)
        stats_block_height = 0
        stats_width = 0
        if stats_lines:
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
        if stats_lines:
            stats_x = max(self._margin, self.width - self._margin - stats_width)
            stats_y = y + max(0, (header_height - stats_block_height) // 2)
            for idx, line in enumerate(stats_lines):
                line_y = stats_y + idx * (stats_font.size + stats_line_gap)
                draw.text((stats_x, line_y), line, font=stats_font, fill=0x00)
        return y + header_height + 30

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
