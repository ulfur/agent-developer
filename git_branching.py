"""Git branching discipline helpers for Nightshift prompt runs."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


class GitBranchError(RuntimeError):
    """Raised when Nightshift cannot enforce the required git workflow."""


@dataclass
class PromptBranchSession:
    branch_name: str
    slug: str
    notes: list[str] = field(default_factory=list)


class PromptBranchDiscipline:
    """Creates per-prompt git branches and cleans them up after runs."""

    def __init__(
        self,
        repo_root: Path,
        *,
        logger: Optional[logging.Logger] = None,
        base_branch: Optional[str] = None,
        branch_prefix: Optional[str] = None,
        slug_words: Optional[int] = None,
        slug_chars: Optional[int] = None,
        cleanup_enabled: Optional[bool] = None,
        allow_dirty_workspace: Optional[bool] = None,
        dry_run_mutations: bool = False,
        discipline_disabled: bool = False,
    ) -> None:
        self.repo_root = repo_root
        self.logger = logger or logging.getLogger("prompt_branch")
        self.base_branch = (base_branch or os.environ.get("NIGHTSHIFT_GIT_BASE_BRANCH") or "dev").strip()
        self.branch_prefix = (
            branch_prefix or os.environ.get("NIGHTSHIFT_PROMPT_BRANCH_PREFIX") or "nightshift/prompt"
        ).strip()
        self.slug_words = slug_words or int(os.environ.get("NIGHTSHIFT_BRANCH_SLUG_WORDS", "6"))
        self.slug_chars = slug_chars or int(os.environ.get("NIGHTSHIFT_BRANCH_SLUG_CHARS", "48"))
        self.cleanup_enabled = (
            cleanup_enabled
            if cleanup_enabled is not None
            else self._env_flag("NIGHTSHIFT_PROMPT_BRANCH_CLEANUP", default=True)
        )
        allow_dirty = (
            allow_dirty_workspace
            if allow_dirty_workspace is not None
            else self._env_flag("NIGHTSHIFT_GIT_ALLOW_DIRTY", default=False)
        )
        self.allow_dirty_workspace = allow_dirty
        self.dry_run_mutations = dry_run_mutations or self._env_flag("NIGHTSHIFT_GIT_DRY_RUN")
        disabled = (
            discipline_disabled
            or self._env_flag("NIGHTSHIFT_DISABLE_BRANCH_DISCIPLINE", default=False)
        )
        self.discipline_disabled = disabled

    @staticmethod
    def _env_flag(name: str, default: bool = False) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def begin_run(self, prompt_id: str, prompt_text: str) -> Optional[PromptBranchSession]:
        if self.discipline_disabled:
            return None
        self._ensure_git_repo()
        if not self.allow_dirty_workspace and self._workspace_dirty():
            raise GitBranchError("Working tree has uncommitted changes; finish or reset the previous prompt first.")
        branch_name, slug = self._branch_name(prompt_id, prompt_text)
        session = PromptBranchSession(branch_name=branch_name, slug=slug)
        self.logger.info("Preparing git branch %s based on %s", session.branch_name, self.base_branch)
        self._ensure_branch_exists(self.base_branch)
        self._git(["switch", self.base_branch], mutating=True)
        if self._branch_exists(session.branch_name):
            self._git(["branch", "-D", session.branch_name], mutating=True)
            session.notes.append(f"Removed stale branch {session.branch_name}")
        self._git(["switch", "-C", session.branch_name, self.base_branch], mutating=True)
        session.notes.append(f"Checked out {session.branch_name} from {self.base_branch}")
        return session

    def finalize_run(self, session: Optional[PromptBranchSession]) -> list[str]:
        if not session or self.discipline_disabled:
            return []
        if not self.cleanup_enabled:
            return [f"Cleanup disabled by config; leaving {session.branch_name} checked out."]
        if self._workspace_dirty():
            raise GitBranchError(
                f"Cannot clean up {session.branch_name}; working tree has uncommitted changes or pending merges."
            )
        self._git(["switch", self.base_branch], mutating=True)
        self._git(["branch", "-D", session.branch_name], mutating=True)
        return [f"Deleted {session.branch_name}; workspace reset to {self.base_branch}"]

    # ------------------------------------------------------------------ helpers
    def _ensure_git_repo(self) -> None:
        try:
            result = self._git(["rev-parse", "--is-inside-work-tree"], capture=True)
        except GitBranchError as exc:
            raise GitBranchError(f"{exc}; run `git init` and create {self.base_branch} first.") from exc
        if result.stdout.strip() != "true":
            raise GitBranchError("Repository root is not a git work tree.")

    def _workspace_dirty(self) -> bool:
        result = self._git(["status", "--porcelain"], capture=True)
        return bool(result.stdout.strip())

    def _branch_name(self, prompt_id: str, prompt_text: str) -> tuple[str, str]:
        slug = self._slugify(prompt_text)
        if slug:
            return f"{self.branch_prefix}-{prompt_id}-{slug}", slug
        return f"{self.branch_prefix}-{prompt_id}", "update"

    def _slugify(self, prompt_text: str) -> str:
        cleaned = prompt_text.lower()
        cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
        words = [word for word in cleaned.strip().split() if word]
        if self.slug_words > 0:
            words = words[: self.slug_words]
        slug = "-".join(words)
        slug = slug[: self.slug_chars].strip("-")
        return slug or "update"

    def _ensure_branch_exists(self, branch: str) -> None:
        try:
            self._git(["rev-parse", "--verify", branch], capture=True)
        except GitBranchError as exc:
            raise GitBranchError(f"Base branch '{branch}' does not exist.") from exc

    def _branch_exists(self, branch: str) -> bool:
        try:
            self._git(["rev-parse", "--verify", branch], capture=True)
            return True
        except GitBranchError:
            return False

    def _git(self, args: List[str], *, mutating: bool = False, capture: bool = False) -> subprocess.CompletedProcess:
        cmd = ["git", *args]
        if self.dry_run_mutations and mutating:
            self.logger.info("[dry-run] git %s", " ".join(args))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_root,
                check=True,
                capture_output=capture,
                text=True,
            )
            return result
        except subprocess.CalledProcessError as exc:  # pragma: no cover - subprocess surfaces message
            stderr = exc.stderr.strip() if exc.stderr else ""
            stdout = exc.stdout.strip() if exc.stdout else ""
            message = stderr or stdout or f"git {' '.join(args)} failed with code {exc.returncode}"
            raise GitBranchError(message) from exc
