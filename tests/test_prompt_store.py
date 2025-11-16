import sys
import tempfile
import unittest
from pathlib import Path


# Ensure backend modules are importable.
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import server  # type: ignore  # pylint: disable=import-error


class PromptStoreEditPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base_path = Path(self.tmpdir.name)
        self.prompts_path = base_path / "prompts.json"
        self.original_log_dir = server.LOG_DIR
        server.LOG_DIR = base_path / "logs"
        server.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.store = server.PromptStore(self.prompts_path)

    def tearDown(self) -> None:
        server.LOG_DIR = self.original_log_dir
        self.tmpdir.cleanup()

    def _transition_status(self, prompt_id: str, status: str) -> None:
        if status == "failed":
            self.store.mark_failed(prompt_id, "failed for test")
        elif status == "completed":
            self.store.mark_completed(prompt_id, "done")
        elif status == "canceled":
            self.store.mark_canceled(prompt_id, "canceled by test")

    def test_update_prompt_text_allows_terminal_statuses(self) -> None:
        for status in ("queued", "failed", "completed", "canceled"):
            record = self.store.add_prompt(f"original text for {status}")
            if status != "queued":
                self._transition_status(record.prompt_id, status)
            updated = self.store.update_prompt_text(record.prompt_id, f"updated {status}")
            self.assertEqual(updated.text, f"updated {status}")
            self.assertEqual(updated.status, status)

    def test_update_prompt_text_rejects_running_prompt(self) -> None:
        record = self.store.add_prompt("original text for running prompt")
        self.store.begin_attempt(record.prompt_id)
        with self.assertRaises(ValueError):
            self.store.update_prompt_text(record.prompt_id, "should fail while running")


if __name__ == "__main__":
    unittest.main()
