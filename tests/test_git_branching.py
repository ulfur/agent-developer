import subprocess
import tempfile
import unittest
from pathlib import Path

from git_branching import GitBranchError, PromptBranchDiscipline


class PromptBranchDisciplineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.tempdir.name)
        self._init_repo()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _git(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
            text=True,
        )

    def _init_repo(self) -> None:
        self._git(["init"])
        self._git(["config", "user.name", "Nightshift"])
        self._git(["config", "user.email", "nightshift@example.com"])
        (self.repo_root / "README.md").write_text("root\n", encoding="utf-8")
        self._git(["add", "README.md"])
        self._git(["commit", "-m", "init"])
        self._git(["checkout", "-b", "dev"])

    def test_begin_and_finalize_run_creates_branch_from_dev(self) -> None:
        discipline = PromptBranchDiscipline(self.repo_root)
        session = discipline.begin_run("abc123", "Add telemetry counters")
        self.assertIsNotNone(session)
        current_branch = self._git(["branch", "--show-current"]).stdout.strip()
        self.assertEqual(current_branch, session.branch_name)
        (self.repo_root / "README.md").write_text("branch update\n", encoding="utf-8")
        self._git(["commit", "-am", "prompt commit"])
        cleanup = discipline.finalize_run(session)
        self.assertIsNotNone(cleanup)
        assert cleanup is not None  # hint for type checkers
        self.assertTrue(any("Deleted" in note for note in cleanup.notes))
        self.assertEqual(len(cleanup.commits), 1)
        current_branch = self._git(["branch", "--show-current"]).stdout.strip()
        self.assertEqual(current_branch, "dev")
        branches = self._git(["branch"]).stdout
        self.assertNotIn(session.branch_name, branches)
        log_message = self._git(["log", "-1", "--pretty=%s"]).stdout.strip()
        self.assertEqual(log_message, "prompt commit")

    def test_begin_run_rejects_dirty_workspace(self) -> None:
        (self.repo_root / "README.md").write_text("dirty\n", encoding="utf-8")
        discipline = PromptBranchDiscipline(self.repo_root)
        with self.assertRaises(GitBranchError):
            discipline.begin_run("abc123", "Dirty workspace rejection")

    def test_finalize_run_fails_when_workspace_dirty(self) -> None:
        discipline = PromptBranchDiscipline(self.repo_root)
        session = discipline.begin_run("abc123", "Edit files")
        (self.repo_root / "README.md").write_text("pending\n", encoding="utf-8")
        with self.assertRaises(GitBranchError):
            discipline.finalize_run(session)

    def test_rollback_prompt_commits(self) -> None:
        discipline = PromptBranchDiscipline(self.repo_root)
        session = discipline.begin_run("abc123", "Add telemetry counters")
        (self.repo_root / "README.md").write_text("branch update\n", encoding="utf-8")
        self._git(["commit", "-am", "prompt commit"])
        cleanup = discipline.finalize_run(session)
        assert cleanup is not None
        self.assertGreater(len(cleanup.commits), 0)
        content_after = (self.repo_root / "README.md").read_text(encoding="utf-8")
        self.assertEqual(content_after, "branch update\n")
        rollback = discipline.rollback_prompt_commits(
            "abc123",
            "Add telemetry counters",
            cleanup.commits,
            slug=session.slug,
        )
        self.assertIsNotNone(rollback.rollback_commit)
        reverted = (self.repo_root / "README.md").read_text(encoding="utf-8")
        self.assertEqual(reverted, "root\n")


if __name__ == "__main__":
    unittest.main()
