#!/usr/bin/env python3
"""Workspace scope enforcement wrapper for Codex CLI runs.

This helper proxies Codex CLI invocations, tracks the files mutated by
`apply_patch`/`shell` commands, and rejects edits that fall outside the active
project's scope manifest. Violations are logged to `logs/scope_violations.log`,
summaries are written to a status file for the backend, and offending changes
are reverted before the guard terminates the Codex process.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional


COMMAND_EXIT_RE = re.compile(r"^(?P<command>.+?) exited (?P<code>-?\d+) in (?P<duration>[0-9.]+)ms:")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_manifest() -> dict:
    raw = os.environ.get("CODEX_SCOPE_MANIFEST", "")
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise SystemExit(f"Invalid CODEX_SCOPE_MANIFEST payload: {exc}")
    if not isinstance(payload, dict):
        return {}
    return payload


class DirtyFileTracker:
    """Tracks modified/untracked files via git metadata and mtimes."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self._state: Dict[str, tuple[bool, int, int]] = {}
        self._lock = threading.Lock()
        self._state = self._snapshot()

    def _git_list(self, args: list[str]) -> list[str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(stderr or "git command failed")
        output = proc.stdout.decode("utf-8", errors="ignore")
        entries = [entry for entry in output.split("\0") if entry]
        return entries

    def _file_state(self, relative_path: str) -> tuple[bool, int, int]:
        candidate = (self.repo_root / relative_path).resolve()
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            return (False, 0, 0)
        return (True, stat.st_mtime_ns, stat.st_size)

    def _snapshot(self) -> Dict[str, tuple[bool, int, int]]:
        snapshot: Dict[str, tuple[bool, int, int]] = {}
        tracked = self._git_list(["ls-files", "-m", "-z"])
        untracked = self._git_list(["ls-files", "-o", "--exclude-standard", "-z"])
        deleted = self._git_list(["ls-files", "-d", "-z"])
        for path in tracked:
            snapshot[path] = self._file_state(path)
        for path in untracked:
            snapshot.setdefault(path, self._file_state(path))
        for path in deleted:
            snapshot[path] = (False, 0, 0)
        return snapshot

    def scan(self) -> list[str]:
        with self._lock:
            new_state = self._snapshot()
            changed_paths: set[str] = set(self._state.keys()) | set(new_state.keys())
            touched = sorted(path for path in changed_paths if self._state.get(path) != new_state.get(path))
            self._state = new_state
        return touched

    def refresh(self) -> None:
        with self._lock:
            self._state = self._snapshot()


class ScopeGuard:
    def __init__(
        self,
        repo_root: Path,
        manifest: dict,
        prompt_id: str,
        project_id: str,
        status_path: Path,
        violation_log: Path,
    ) -> None:
        self.repo_root = repo_root
        self.prompt_id = prompt_id or ""
        self.project_id = project_id or ""
        self.status_path = status_path
        self.violation_log = violation_log
        self.allow_patterns = self._normalize_patterns(manifest.get("allow", []))
        self.deny_patterns = self._normalize_patterns(manifest.get("deny", []))
        self.log_only_patterns = self._normalize_patterns(manifest.get("log_only", []))
        description = str(manifest.get("description") or "").strip()
        self.description = description
        self._violation_info: dict[str, object] | None = None

    @staticmethod
    def _normalize_patterns(patterns: Iterable[str]) -> list[str]:
        normalized: list[str] = []
        for pattern in patterns:
            if pattern is None:
                continue
            cleaned = str(pattern).strip().lstrip("./")
            if cleaned:
                normalized.append(cleaned.replace("\\", "/"))
        return normalized

    def _normalize_path(self, relative: str) -> str:
        cleaned = relative.replace("\\", "/")
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        return cleaned

    def _matches(self, relative: str, patterns: Iterable[str]) -> bool:
        from fnmatch import fnmatchcase

        return any(fnmatchcase(relative, pattern) for pattern in patterns)

    def classify_path(self, relative: str) -> str:
        rel = self._normalize_path(relative)
        if not rel:
            return "deny"
        if self._matches(rel, self.deny_patterns):
            return "deny"
        allowed = bool(self.allow_patterns)
        if not self.allow_patterns:
            allowed = True
        else:
            allowed = self._matches(rel, self.allow_patterns)
        if not allowed:
            return "deny"
        if self._matches(rel, self.log_only_patterns):
            return "log_only"
        return "allow"

    def find_violations(self, paths: Iterable[str]) -> list[str]:
        violations: list[str] = []
        for path in paths:
            if self.classify_path(path) == "deny":
                violations.append(path)
        return violations

    @property
    def violated(self) -> bool:
        return self._violation_info is not None

    def _write_status_file(self, payload: dict[str, object]) -> None:
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            self.status_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _append_violation_log(self, paths: Iterable[str], command: str, timestamp: str) -> None:
        try:
            self.violation_log.parent.mkdir(parents=True, exist_ok=True)
            with self.violation_log.open("a", encoding="utf-8") as handle:
                for path in paths:
                    record = {
                        "timestamp": timestamp,
                        "prompt_id": self.prompt_id,
                        "project_id": self.project_id,
                        "path": self._normalize_path(path),
                        "command": command,
                    }
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _restore_with_git(self, relative: str) -> bool:
        proc = subprocess.run(
            ["git", "checkout", "--", relative],
            cwd=self.repo_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return proc.returncode == 0

    def _delete_path(self, relative: str) -> None:
        target = (self.repo_root / relative).resolve()
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        except OSError:
            pass

    def revert_paths(self, paths: Iterable[str]) -> None:
        for path in paths:
            restored = self._restore_with_git(path)
            if not restored:
                self._delete_path(path)

    def handle_violation(self, command: str, paths: list[str]) -> str:
        if self._violation_info:
            return str(self._violation_info.get("message", "Scope guard violation"))
        timestamp = utcnow_iso()
        normalized = [self._normalize_path(path) for path in paths]
        message = (
            f"Scope guard blocked {command} touching disallowed paths: "
            + ", ".join(normalized)
        )
        payload = {
            "timestamp": timestamp,
            "prompt_id": self.prompt_id,
            "project_id": self.project_id,
            "paths": normalized,
            "message": message,
            "command": command,
        }
        self._violation_info = payload
        self._append_violation_log(paths, command, timestamp)
        self._write_status_file(payload)
        self.revert_paths(paths)
        print(message, file=sys.stdout, flush=True)
        return message

    def handle_guard_failure(self, reason: str) -> None:
        if self._violation_info:
            return
        timestamp = utcnow_iso()
        message = f"Scope guard error: {reason.strip() or 'unknown failure'}"
        payload = {
            "timestamp": timestamp,
            "prompt_id": self.prompt_id,
            "project_id": self.project_id,
            "paths": [],
            "message": message,
            "command": "<guard>",
        }
        self._violation_info = payload
        self._write_status_file(payload)
        print(message, file=sys.stdout, flush=True)


class CommandMonitor:
    def __init__(
        self,
        tracker: Optional[DirtyFileTracker],
        guard: ScopeGuard,
        terminate_callback,
    ) -> None:
        self.tracker = tracker
        self.guard = guard
        self.terminate_callback = terminate_callback

    def process_line(self, line: str) -> None:
        if self.guard.violated or not self.tracker:
            return
        match = COMMAND_EXIT_RE.match(line.strip())
        if not match:
            return
        try:
            changed_paths = self.tracker.scan()
        except RuntimeError as exc:
            self.guard.handle_guard_failure(str(exc))
            if self.terminate_callback:
                self.terminate_callback()
            return
        if not changed_paths:
            return
        violations = self.guard.find_violations(changed_paths)
        if not violations:
            return
        self.guard.handle_violation(match.group("command"), violations)
        if self.terminate_callback:
            self.terminate_callback()
        self.tracker.refresh()


class GuardedProcess:
    def __init__(
        self,
        inner_cmd: list[str],
        prompt_text: str,
        repo_root: Path,
        guard: ScopeGuard,
        tracker: Optional[DirtyFileTracker],
    ) -> None:
        self.inner_cmd = inner_cmd
        self.prompt_text = prompt_text
        self.repo_root = repo_root
        self.guard = guard
        self.tracker = tracker
        self.process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._terminate_lock = threading.Lock()

    def _terminate_child(self) -> None:
        with self._terminate_lock:
            if not self.process:
                return
            if self.process.poll() is not None:
                return
            try:
                self.process.terminate()
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

    def _pump_stream(self, stream, write_fn, monitor: Optional[CommandMonitor] = None) -> None:
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    continue
                write_fn(line)
                if monitor:
                    monitor.process_line(line)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def run(self) -> int:
        monitor = CommandMonitor(self.tracker, self.guard, self._terminate_child)
        self.process = subprocess.Popen(
            self.inner_cmd,
            cwd=self.repo_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self.process.stdin is not None
        self.process.stdin.write(self.prompt_text)
        self.process.stdin.close()

        self._stdout_thread = threading.Thread(
            target=self._pump_stream,
            args=(self.process.stdout, lambda chunk: print(chunk, end=""), monitor),
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._pump_stream,
            args=(self.process.stderr, lambda chunk: print(chunk, end="", file=sys.stderr)),
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        return_code = self.process.wait()
        self._stdout_thread.join()
        self._stderr_thread.join()

        if self.guard.violated:
            return 86
        return return_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Scope-guarded Codex CLI wrapper")
    parser.add_argument("inner", nargs=argparse.REMAINDER, help="Inner Codex CLI command")
    args = parser.parse_args()
    if not args.inner:
        print("scope_guard.py requires an inner Codex CLI command", file=sys.stderr)
        return 2

    prompt_payload = sys.stdin.read()
    repo_root = Path(os.environ.get("CODEX_SCOPE_REPO_ROOT", os.getcwd())).resolve()
    prompt_id = os.environ.get("CODEX_SCOPE_PROMPT_ID", "")
    project_id = os.environ.get("CODEX_SCOPE_PROJECT_ID", "")
    status_env = os.environ.get("CODEX_SCOPE_STATUS_PATH", "")
    if status_env.strip():
        status_path = Path(status_env)
    else:
        status_path = repo_root / "logs" / f"scope_guard_{prompt_id or 'run'}.json"
    violation_path = Path(
        os.environ.get(
            "CODEX_SCOPE_VIOLATION_LOG",
            repo_root / "logs/scope_violations.log",
        )
    )

    manifest = read_manifest()
    guard = ScopeGuard(repo_root, manifest, prompt_id, project_id, status_path, violation_path)

    try:
        tracker = DirtyFileTracker(repo_root)
    except RuntimeError as exc:
        guard.handle_guard_failure(str(exc))
        return 86

    runner = GuardedProcess(args.inner, prompt_payload, repo_root, guard, tracker)
    return runner.run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
