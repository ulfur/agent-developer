"""Shared helpers for parsing Codex execution logs."""

from __future__ import annotations

from pathlib import Path
from typing import Union


PathLike = Union[str, Path]


def extract_stdout_preview(log_path: PathLike) -> str:
    """Return the most recent Codex stdout section from a log file."""
    path = Path(log_path)
    if not path.exists():
        return ""
    try:
        log_text = path.read_text(encoding="utf-8")
    except OSError:
        return ""

    marker = "Codex stdout:"
    stderr_marker = "Codex stderr:"
    idx = log_text.rfind(marker)
    if idx == -1:
        return ""

    after = log_text[idx + len(marker) :]
    if stderr_marker in after:
        after = after.split(stderr_marker, 1)[0]
    return after.strip()
