import json
import tempfile
import unittest
from pathlib import Path

from scripts.plan_prompt_queue import PromptQueue, UpgradePlan, queue_plan_tasks


class PlanPromptQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _write_plan(self, tasks):
        payload = {"tasks": tasks, "generated_at": "2025-11-15T00:00:00Z"}
        plan_path = self.temp_path / "plan.json"
        plan_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return plan_path

    def test_pending_tasks_only_includes_pending_entries(self) -> None:
        tasks = [
            {"id": "one", "title": "Task A", "prompt": "Do A", "status": "pending"},
            {"id": "two", "title": "Task B", "prompt": "Do B", "status": "queued"},
            {"id": "three", "title": "Task C", "prompt": "Do C"},
        ]
        plan_path = self._write_plan(tasks)
        plan = UpgradePlan(plan_path)
        pending_ids = [task.task_id for task in plan.pending_tasks()]
        self.assertEqual(pending_ids, ["one", "three"])

    def test_queue_plan_tasks_updates_prompts_file_and_plan(self) -> None:
        tasks = [
            {"id": "a", "title": "Task A", "prompt": "Do something A", "project_id": "agent-dev-host", "status": "pending"},
            {"id": "b", "title": "Task B", "prompt": "Do something B", "project_id": "agent-dev-host", "status": "pending"},
            {"id": "c", "title": "Task C", "prompt": "Do something C", "project_id": "agent-dev-host", "status": "pending"},
        ]
        plan_path = self._write_plan(tasks)
        prompts_path = self.temp_path / "prompts.json"
        logs_dir = self.temp_path / "logs"
        plan = UpgradePlan(plan_path)
        queue = PromptQueue(prompts_path, logs_dir)

        results = queue_plan_tasks(plan, queue, limit=2)

        self.assertEqual(len(results), 2)
        prompts_data = json.loads(prompts_path.read_text(encoding="utf-8"))
        self.assertEqual(len(prompts_data), 2)
        for record in prompts_data.values():
            self.assertEqual(record["status"], "queued")
            self.assertTrue(record["log_path"].startswith(str(logs_dir)))
            self.assertEqual(record["project_id"], "agent-dev-host")

        updated_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        queued_tasks = [task for task in updated_plan["tasks"] if task["status"] == "queued"]
        self.assertEqual(len(queued_tasks), 2)
        for task in queued_tasks:
            self.assertEqual(task["queued_count"], 1)
            self.assertIn("last_prompt_id", task)
            self.assertIn(task["last_prompt_id"], prompts_data)
            self.assertTrue(task["last_queued_at"])
        remaining = [task for task in updated_plan["tasks"] if task["status"] == "pending"]
        self.assertEqual(len(remaining), 1)


if __name__ == "__main__":
    unittest.main()
