from backend.environments import EnvironmentRecord
from backend.router_config import RouterConfigBuilder


def _record_with_router(metadata: dict, *, ports: list[dict] | None = None) -> EnvironmentRecord:
    return EnvironmentRecord(
        environment_id="env-nightshift-dev",
        project_id="nightshift",
        slug="nightshift-dev",
        name="Nightshift Dev",
        description="",
        host={
            "hostname": "pi-dev.nightshift.local",
            "provider": "pi",
            "region": "lab",
            "ip": "10.0.0.5",
            "notes": "",
        },
        ports=ports or [],
        owner={"name": "Ops", "email": "", "slack": "", "role": ""},
        lifecycle={"state": "active", "changed_at": "", "notes": ""},
        health={"status": "healthy", "checked_at": "", "url": "", "notes": ""},
        metadata=metadata,
    )


def test_router_config_includes_tls_entry(tmp_path):
    cert_dir = tmp_path / "certs"
    cert_dir.mkdir()
    (cert_dir / "nightshift-dev.crt").write_text("CERT", encoding="utf-8")
    (cert_dir / "nightshift-dev.key").write_text("KEY", encoding="utf-8")
    record = _record_with_router(
        {
            "router": {
                "hostnames": ["dev.nightshift.local"],
                "service_url": "http://pi-dev.nightshift.local:8080",
                "entrypoints": ["websecure"],
                "tls": {
                    "cert_file": "nightshift-dev.crt",
                    "key_file": "nightshift-dev.key",
                    "domains": ["dev.nightshift.local"],
                },
            }
        }
    )
    builder = RouterConfigBuilder(certs_dir=cert_dir, certs_mount_path="/etc/traefik/certs")
    config, warnings = builder.build([record])
    assert not warnings
    routers = config["http"]["routers"]
    router = routers["nightshift-dev"]
    assert router["entryPoints"] == ["websecure"]
    assert router["service"] == "nightshift-dev-svc"
    tls_entry = config["tls"]["certificates"][0]
    assert tls_entry["certFile"] == "/etc/traefik/certs/nightshift-dev.crt"
    assert tls_entry["keyFile"] == "/etc/traefik/certs/nightshift-dev.key"


def test_router_config_falls_back_to_port_metadata(tmp_path):
    record = _record_with_router(
        {"router": {"hostnames": ["env.local"], "port": "http", "entrypoints": ["web"]}},
        ports=[
            {"name": "http", "port": 8080, "protocol": "http", "url": ""},
        ],
    )
    builder = RouterConfigBuilder(certs_dir=tmp_path)
    config, warnings = builder.build([record])
    assert not warnings
    service = config["http"]["services"]["nightshift-dev-svc"]
    servers = service["loadBalancer"]["servers"]
    assert servers == [{"url": "http://pi-dev.nightshift.local:8080"}]


def test_router_warns_when_tls_files_missing(tmp_path):
    record = _record_with_router(
        {
            "router": {
                "hostnames": ["env.local"],
                "service_url": "http://pi-dev.nightshift.local:8080",
                "tls": {"cert_file": "missing.crt", "key_file": "missing.key"},
            }
        }
    )
    builder = RouterConfigBuilder(certs_dir=tmp_path)
    config, warnings = builder.build([record])
    assert warnings
    router = config["http"]["routers"]["nightshift-dev"]
    assert "tls" not in router
