import sys
import tempfile
import unittest
from pathlib import Path


# Ensure backend modules are importable.
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import server  # type: ignore  # pylint: disable=import-error


class PromptStoreTestCase(unittest.TestCase):
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


class PromptStoreEditPromptTests(PromptStoreTestCase):
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


class PromptStoreListPromptTests(PromptStoreTestCase):
    def test_list_prompts_orders_and_assigns_positions(self) -> None:
        queued_a = self.store.add_prompt("queued first")
        queued_b = self.store.add_prompt("queued second")
        running_a = self.store.add_prompt("running 1")
        running_b = self.store.add_prompt("running 2")
        completed = self.store.add_prompt("completed prompt")
        failed = self.store.add_prompt("failed prompt")

        self.store.begin_attempt(running_a.prompt_id)
        self.store.begin_attempt(running_b.prompt_id)
        self.store.mark_completed(completed.prompt_id, "done")
        self.store.mark_failed(failed.prompt_id, "boom")

        with self.store._lock:
            self.store._records[queued_a.prompt_id].enqueued_at = "2025-01-01T00:00:00+00:00"
            self.store._records[queued_b.prompt_id].enqueued_at = "2025-01-01T00:00:05+00:00"
            self.store._records[running_a.prompt_id].started_at = "2025-01-01T00:00:10+00:00"
            self.store._records[running_b.prompt_id].started_at = "2025-01-01T00:00:15+00:00"
            self.store._records[completed.prompt_id].updated_at = "2025-01-01T00:01:00+00:00"
            self.store._records[failed.prompt_id].updated_at = "2025-01-01T00:02:00+00:00"

        snapshot = self.store.list_prompts()
        ordered_ids = [item["prompt_id"] for item in snapshot["items"]]
        self.assertEqual(
            ordered_ids,
            [
                queued_a.prompt_id,
                queued_b.prompt_id,
                running_a.prompt_id,
                running_b.prompt_id,
                failed.prompt_id,
                completed.prompt_id,
            ],
        )

        positions = [item.get("queue_position") for item in snapshot["items"]]
        self.assertEqual(positions[:4], [1, 2, 3, 4])
        self.assertTrue(all(pos is None for pos in positions[4:]))

        buckets = snapshot["status_buckets"]
        self.assertEqual(buckets["queued"]["count"], 2)
        self.assertEqual(buckets["queued"]["prompt_ids"], [queued_a.prompt_id, queued_b.prompt_id])
        self.assertEqual(buckets["running"]["count"], 2)
        self.assertEqual(buckets["running"]["prompt_ids"], [running_a.prompt_id, running_b.prompt_id])
        self.assertEqual(buckets["failed"]["count"], 1)
        self.assertEqual(buckets["completed"]["count"], 1)
        self.assertEqual(buckets["canceled"]["count"], 0)

    def test_server_restart_items_reported_in_running_bucket(self) -> None:
        queued = self.store.add_prompt("queued prompt")
        running = self.store.add_prompt("running prompt")
        restarting = self.store.add_prompt("restarting prompt")

        self.store.begin_attempt(running.prompt_id)
        self.store.begin_attempt(restarting.prompt_id)
        self.store.mark_server_restarting(restarting.prompt_id, summary="restart", requires_follow_up=False)

        with self.store._lock:
            self.store._records[queued.prompt_id].enqueued_at = "2025-01-01T00:00:00+00:00"
            self.store._records[running.prompt_id].started_at = "2025-01-01T00:00:05+00:00"
            restarting_record = self.store._records[restarting.prompt_id]
            restarting_record.server_restart_marked_at = "2025-01-01T00:00:10+00:00"

        snapshot = self.store.list_prompts()
        ordered_ids = [item["prompt_id"] for item in snapshot["items"][:3]]
        self.assertEqual(
            ordered_ids,
            [queued.prompt_id, running.prompt_id, restarting.prompt_id],
        )
        self.assertEqual(
            [item.get("queue_position") for item in snapshot["items"][:3]],
            [1, 2, 3],
        )
        restart_bucket = snapshot["status_buckets"][server.PROMPT_STATUS_SERVER_RESTARTING]
        self.assertEqual(restart_bucket["count"], 1)
        self.assertEqual(restart_bucket["prompt_ids"], [restarting.prompt_id])
        self.assertEqual(snapshot["status_buckets"]["running"]["count"], 1)


class PromptStoreReplyReferenceTests(PromptStoreTestCase):
    def test_add_prompt_records_reply_reference(self) -> None:
        base_prompt = self.store.add_prompt("initial work")
        self.store.mark_completed(base_prompt.prompt_id, "done")
        follow_up = self.store.add_prompt("follow up", reply_to_prompt_id=base_prompt.prompt_id)
        self.assertEqual(follow_up.reply_to_prompt_id, base_prompt.prompt_id)
        snapshot = self.store.list_prompts()
        follow_up_entry = next(
            item for item in snapshot["items"] if item["prompt_id"] == follow_up.prompt_id
        )
        self.assertEqual(follow_up_entry["reply_to_prompt_id"], base_prompt.prompt_id)


if __name__ == "__main__":
    unittest.main()
