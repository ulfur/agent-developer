from pathlib import Path

import pytest

from backend.environments import EnvironmentStore


def build_payload(**overrides):
    payload = {
        "project_id": "nightshift",
        "slug": "nightshift-dev",
        "name": "Nightshift Dev",
        "description": "Primary dev host",
        "host": {
            "hostname": "dev.local",
            "provider": "lab",
            "region": "loc",
            "ip": "10.0.0.5",
            "notes": "",
        },
        "owner": {
            "name": "Ulfur",
            "email": "ops@example.com",
            "slack": "#ops",
            "role": "Operator",
        },
        "ports": [
            {"name": "http", "port": 8080, "protocol": "tcp", "url": "", "description": ""}
        ],
        "lifecycle": {"state": "active", "notes": ""},
        "health": {"status": "healthy", "url": "", "notes": "", "checked_at": "2025-11-16T10:00:00+00:00"},
    }
    payload.update(overrides)
    return payload


def create_store(tmp_path: Path, registry=None) -> EnvironmentStore:
    path = tmp_path / "environments.json"
    return EnvironmentStore(path, registry)


def test_create_environment_enforces_unique_slug(tmp_path):
    store = create_store(tmp_path)
    store.create_environment(build_payload())
    with pytest.raises(ValueError):
        store.create_environment(build_payload(name="Duplicate"))


def test_update_environment_merges_host_metadata(tmp_path):
    store = create_store(tmp_path)
    record = store.create_environment(build_payload())
    updated = store.update_environment(record.environment_id, {"host": {"hostname": "dev.local", "provider": "lab", "region": "loc", "ip": "10.0.0.5", "notes": "Rack A"}})
    assert updated.host["notes"] == "Rack A"


def test_list_environments_filters_by_project(tmp_path):
    store = create_store(tmp_path)
    store.create_environment(build_payload(slug="nightshift-dev"))
    store.create_environment(build_payload(slug="nightshift-preview", project_id="nightshift"))
    store.create_environment(build_payload(slug="nebulapulse-demo", project_id="nebulapulse", name="Nebula"))
    scoped = store.list_environments(project_id="nebulapulse")
    assert len(scoped) == 1
    assert scoped[0].slug == "nebulapulse-demo"


def test_health_snapshot_counts_statuses(tmp_path):
    store = create_store(tmp_path)
    store.create_environment(build_payload(slug="env-a", health={"status": "healthy", "checked_at": "2025-11-16T10:00:00+00:00", "url": "", "notes": ""}))
    store.create_environment(
        build_payload(
            slug="env-b",
            health={"status": "degraded", "checked_at": "2024-01-01T00:00:00+00:00", "url": "", "notes": ""},
            project_id="nebulapulse",
        )
    )
    metrics = store.health_snapshot()
    assert metrics["total"] == 2
    assert metrics["status_counts"]["healthy"] == 1
    assert metrics["status_counts"]["degraded"] == 1
    assert metrics["stale_checks"] >= 1
    assert "revision" in metrics


def test_health_snapshot_reports_revision(tmp_path):
    store = create_store(tmp_path)
    baseline = store.health_snapshot()
    assert baseline["revision"] == 0
    store.create_environment(build_payload(slug="env-rev"))
    updated = store.health_snapshot()
    assert updated["revision"] == baseline["revision"] + 1


def test_collection_payload_includes_revision_and_project_payload(tmp_path):
    class DummyProject:
        def __init__(self, payload):
            self._payload = payload

        def to_payload(self):
            return self._payload

    class DummyRegistry:
        def __init__(self):
            self._projects = {"nightshift": DummyProject({"id": "nightshift", "name": "Nightshift"})}

        def get(self, project_id):
            return self._projects.get(project_id)

    registry = DummyRegistry()
    store = create_store(tmp_path, registry=registry)
    store.create_environment(build_payload())
    payload = store.to_collection_payload(registry)
    assert payload["revision"] == 1
    assert payload["total"] == 1
    entry = payload["environments"][0]
    assert entry["project"]["id"] == "nightshift"
    assert entry["health"]["age_seconds"] is not None
