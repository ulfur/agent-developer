from backend.human_tasks import HumanTaskStore
import pytest


def create_store(tmp_path):
    db_path = tmp_path / "human_tasks.json"
    return HumanTaskStore(db_path)


def test_clear_tasks_removes_all_entries(tmp_path):
    store = create_store(tmp_path)
    store.create_task("One", "desc")
    store.create_task("Two", "desc", status="resolved")

    cleared = store.clear_tasks()

    assert cleared == 2
    assert store.list_tasks() == []


def test_clear_tasks_with_status_filter(tmp_path):
    store = create_store(tmp_path)
    open_task = store.create_task("Open blocker", "desc", status="open")
    store.create_task("Resolved task", "desc", status="resolved")

    cleared = store.clear_tasks(statuses=["resolved"])

    remaining = store.list_tasks()
    assert cleared == 1
    assert len(remaining) == 1
    assert remaining[0].task_id == open_task.task_id


def test_clear_tasks_rejects_invalid_status(tmp_path):
    store = create_store(tmp_path)
    store.create_task("Task", "desc")

    with pytest.raises(ValueError):
        store.clear_tasks(statuses=["invalid"])
