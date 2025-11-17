import logging
from pathlib import Path

import yaml

from backend.agent_identity import AgentIdentityManager, AgentIdentityError


def make_manager(tmp_path: Path) -> AgentIdentityManager:
    identity_path = tmp_path / "agent_identity.yml"
    pairing_path = tmp_path / "pairing_state.json"
    return AgentIdentityManager(identity_path, pairing_path, logger=logging.getLogger("test"))


def test_generates_pairing_code_when_unpaired(tmp_path):
    manager = make_manager(tmp_path)
    payload = manager.public_payload()
    assert payload["status"] == "pairing"
    assert payload["pairing_code"]
    assert payload["instructions_url"]
    assert not manager.is_paired()


def test_accept_registration_persists_identity(tmp_path):
    manager = make_manager(tmp_path)
    pairing = manager.public_payload()
    bundle = {
        "pairing_code": pairing["pairing_code"],
        "agent": {"id": "agent-dev", "name": "Nightshift Dev"},
        "control_plane": {"auth_token": "secret-token", "api_base": "https://api.test.local"},
        "permissions": {"repos": {"allow": ["projects/nightshift"], "deny": []}},
        "cloudflare": {"hostname": "agent-dev.nghtshft.ai"},
    }
    saved = manager.accept_registration(bundle)
    assert manager.is_paired()
    stored = yaml.safe_load((tmp_path / "agent_identity.yml").read_text())
    assert stored["agent"]["id"] == "agent-dev"
    assert stored["cloudflare"]["hostname"] == "agent-dev.nghtshft.ai"
    assert saved["agent"]["name"] == "Nightshift Dev"


def test_refresh_remote_config_merges_payload(tmp_path):
    manager = make_manager(tmp_path)
    pairing = manager.public_payload()
    base_bundle = {
        "pairing_code": pairing["pairing_code"],
        "agent": {"id": "agent-dev", "name": "Nightshift Dev"},
        "control_plane": {"auth_token": "secret-token", "api_base": "https://api.test.local"},
        "permissions": {"repos": {"allow": ["projects/nightshift"], "deny": []}},
        "cloudflare": {"hostname": "agent-dev.nghtshft.ai"},
    }
    manager.accept_registration(base_bundle)

    def fake_fetch(self, url, token):
        assert token == "secret-token"
        assert url.endswith("/agent/agent-dev/config")
        return {"cloudflare": {"hostname": "agent-prod.nghtshft.ai"}}

    manager._fetch_remote_config = fake_fetch.__get__(manager, AgentIdentityManager)  # type: ignore[attr-defined]
    manager.refresh_remote_config()
    refreshed = yaml.safe_load((tmp_path / "agent_identity.yml").read_text())
    assert refreshed["cloudflare"]["hostname"] == "agent-prod.nghtshft.ai"
    assert refreshed["control_plane"]["auth_token"] == "secret-token"


def test_refresh_without_pairing_raises(tmp_path):
    manager = make_manager(tmp_path)
    try:
        manager.refresh_remote_config()
    except AgentIdentityError as exc:
        assert "not registered" in str(exc)
    else:
        raise AssertionError("Expected AgentIdentityError")
