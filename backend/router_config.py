"""Generate Traefik routing config from the environment registry."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

import yaml

from environments import EnvironmentRecord, EnvironmentStore


def _dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _slugify(value: str, *, fallback: str = "env") -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return cleaned or fallback


def _normalize_path_prefix(prefix: Optional[str]) -> str:
    if not prefix:
        return "/"
    text = prefix.strip()
    if not text.startswith("/"):
        text = f"/{text}"
    return text or "/"


def _build_rule(hostnames: Sequence[str], path_prefix: str) -> str:
    host_rules = " || ".join(f"Host(`{host}`)" for host in hostnames)
    if not host_rules:
        return ""
    if path_prefix and path_prefix != "/":
        return f"({host_rules}) && PathPrefix(`{path_prefix}`)"
    return host_rules


def _port_to_url(record: EnvironmentRecord, port_name: Optional[str]) -> Optional[str]:
    selected = None
    if port_name:
        for entry in record.ports:
            if entry.get("name") == port_name:
                selected = entry
                break
    if selected is None:
        for entry in record.ports:
            protocol = (entry.get("protocol") or "tcp").lower()
            if protocol in ("http", "https"):
                selected = entry
                break
    if not selected:
        return None
    url = (selected.get("url") or "").strip()
    if url:
        return url
    hostname = (record.host.get("hostname") or record.host.get("ip") or "").strip()
    if not hostname:
        return None
    protocol = (selected.get("protocol") or "http").lower()
    scheme = "https" if protocol == "https" else "http"
    port_value = selected.get("port")
    if not port_value:
        return None
    return f"{scheme}://{hostname}:{port_value}"


def _ensure_http_scheme(url: str) -> Optional[str]:
    trimmed = url.strip()
    if not trimmed:
        return None
    if not re.match(r"^https?://", trimmed, re.IGNORECASE):
        return None
    return trimmed


@dataclass
class RouterTLSConfig:
    cert_file: str
    key_file: str
    domains: List[str]


@dataclass
class RouterDefinition:
    router_name: str
    service_name: str
    hostnames: List[str]
    entrypoints: List[str]
    path_prefix: str
    strip_prefix: bool
    server_urls: List[str]
    pass_host_header: bool
    middleware_names: List[str]
    tls: Optional[RouterTLSConfig]

    @property
    def rule(self) -> str:
        return _build_rule(self.hostnames, self.path_prefix)


class RouterConfigBuilder:
    """Build Traefik file-provider config from EnvironmentStore records."""

    def __init__(
        self,
        *,
        certs_dir: Path,
        certs_mount_path: str = "/etc/traefik/certs",
    ) -> None:
        self.certs_dir = certs_dir
        self.certs_mount_path = certs_mount_path.rstrip("/") or "/etc/traefik/certs"

    def build(self, records: Sequence[EnvironmentRecord]) -> Tuple[Dict[str, Any], List[str]]:
        routers: Dict[str, Dict[str, Any]] = {}
        services: Dict[str, Dict[str, Any]] = {}
        middlewares: Dict[str, Dict[str, Any]] = {}
        tls_certificates: list[Dict[str, Any]] = []
        warnings: list[str] = []

        for record in sorted(records, key=lambda item: item.slug):
            definition, record_warnings = self._definition_from_record(record)
            warnings.extend(record_warnings)
            if not definition:
                continue
            if not definition.rule:
                warnings.append(f"{record.slug}: unable to build Traefik rule; skipping route")
                continue
            routers[definition.router_name] = {
                "rule": definition.rule,
                "service": definition.service_name,
                "entryPoints": definition.entrypoints,
            }
            if definition.path_prefix != "/" and definition.strip_prefix:
                strip_name = f"{definition.router_name}-strip"
                middlewares[strip_name] = {
                    "stripPrefix": {"prefixes": [definition.path_prefix]}
                }
                routers[definition.router_name]["middlewares"] = [strip_name, *definition.middleware_names]
            elif definition.middleware_names:
                routers[definition.router_name]["middlewares"] = definition.middleware_names

            if definition.tls:
                tls_block: Dict[str, Any] = {}
                if definition.tls.domains:
                    tls_block["domains"] = [
                        {"main": definition.tls.domains[0], "sans": definition.tls.domains[1:]}
                    ]
                routers[definition.router_name]["tls"] = tls_block
                tls_payload = {
                    "certFile": f"{self.certs_mount_path}/{definition.tls.cert_file}",
                    "keyFile": f"{self.certs_mount_path}/{definition.tls.key_file}",
                    "stores": ["default"],
                }
                if definition.tls.domains:
                    tls_payload["domains"] = [
                        {"main": definition.tls.domains[0], "sans": definition.tls.domains[1:]}
                    ]
                tls_certificates.append(tls_payload)

            services[definition.service_name] = {
                "loadBalancer": {
                    "passHostHeader": definition.pass_host_header,
                    "servers": [{"url": url} for url in definition.server_urls],
                }
            }

        config: Dict[str, Any] = {}
        http_section: Dict[str, Any] = {}
        if routers:
            http_section["routers"] = routers
        if services:
            http_section["services"] = services
        if middlewares:
            http_section["middlewares"] = middlewares
        if http_section:
            config["http"] = http_section
        if tls_certificates:
            config["tls"] = {"certificates": tls_certificates}
        if not config:
            config = {"http": {"routers": {}, "services": {}}}
        return config, warnings

    def _definition_from_record(
        self,
        record: EnvironmentRecord,
    ) -> Tuple[Optional[RouterDefinition], List[str]]:
        metadata: Mapping[str, Any] = record.metadata or {}
        router_meta = metadata.get("router")
        warnings: list[str] = []
        if router_meta is None:
            return None, warnings

        if isinstance(router_meta, str):
            router_meta = {"hostnames": [router_meta]}
        if not isinstance(router_meta, MutableMapping):
            warnings.append(f"{record.slug}: router metadata must be a mapping; skipping")
            return None, warnings

        if str(router_meta.get("enabled")).lower() in {"false", "0"}:
            return None, warnings

        router_slug = _slugify(str(router_meta.get("slug") or record.slug or record.environment_id))
        router_name = router_meta.get("router_name") or router_slug
        router_name = _slugify(str(router_name), fallback=router_slug)
        service_name = router_meta.get("service_name") or f"{router_slug}-svc"
        service_name = _slugify(str(service_name), fallback=f"{router_slug}-svc")

        hostnames_raw = router_meta.get("hostnames") or router_meta.get("domains")
        if isinstance(hostnames_raw, str):
            hostnames = [hostnames_raw]
        elif isinstance(hostnames_raw, Sequence):
            hostnames = [str(value) for value in hostnames_raw]
        else:
            fallback_host = record.host.get("hostname")
            hostnames = [fallback_host] if fallback_host else []
        hostnames = [host.strip() for host in hostnames if host and host.strip()]
        hostnames = _dedupe_preserve_order(hostnames)
        if not hostnames:
            warnings.append(f"{record.slug}: router metadata missing hostnames; skipping")
            return None, warnings

        entrypoints_raw = router_meta.get("entrypoints") or router_meta.get("entryPoints")
        entrypoints = (
            [entrypoints_raw]
            if isinstance(entrypoints_raw, str)
            else [str(value) for value in entrypoints_raw]
            if isinstance(entrypoints_raw, Sequence)
            else ["websecure"]
        )
        entrypoints = [value.strip() for value in entrypoints if value and value.strip()]
        if not entrypoints:
            entrypoints = ["websecure"]

        path_prefix = _normalize_path_prefix(router_meta.get("path_prefix") or router_meta.get("pathPrefix"))
        strip_prefix = bool(router_meta.get("strip_prefix", router_meta.get("stripPrefix", path_prefix != "/")))

        middleware_names_raw = router_meta.get("middlewares")
        if isinstance(middleware_names_raw, str):
            middleware_names = [middleware_names_raw]
        elif isinstance(middleware_names_raw, Sequence):
            middleware_names = [str(value) for value in middleware_names_raw]
        else:
            middleware_names = []
        middleware_names = _dedupe_preserve_order(middleware_names)

        pass_host_header = bool(router_meta.get("pass_host_header", router_meta.get("passHostHeader", True)))

        server_urls: list[str] = []
        server_url_override = router_meta.get("service_url") or router_meta.get("server_url")
        if isinstance(server_url_override, str):
            maybe_url = _ensure_http_scheme(server_url_override)
            if maybe_url:
                server_urls.append(maybe_url)
        elif isinstance(server_url_override, Sequence):
            for candidate in server_url_override:
                maybe_url = _ensure_http_scheme(str(candidate or ""))
                if maybe_url:
                    server_urls.append(maybe_url)

        explicit_servers = router_meta.get("server_urls")
        if isinstance(explicit_servers, str):
            maybe = _ensure_http_scheme(explicit_servers)
            if maybe:
                server_urls.append(maybe)
        elif isinstance(explicit_servers, Sequence):
            for candidate in explicit_servers:
                maybe = _ensure_http_scheme(str(candidate or ""))
                if maybe:
                    server_urls.append(maybe)

        port_name = router_meta.get("port") or router_meta.get("port_name")
        if port_name and isinstance(port_name, str):
            resolved = _port_to_url(record, port_name.strip())
            if resolved:
                server_urls.append(resolved)

        if not server_urls:
            resolved = _port_to_url(record, None)
            if resolved:
                server_urls.append(resolved)

        server_urls = _dedupe_preserve_order(server_urls)
        if not server_urls:
            warnings.append(f"{record.slug}: no routable ports found; skipping")
            return None, warnings

        tls_meta = router_meta.get("tls") or {}
        tls_config: Optional[RouterTLSConfig] = None
        if isinstance(tls_meta, Mapping):
            cert_file = str(tls_meta.get("cert_file") or tls_meta.get("certificate") or "").strip()
            key_file = str(tls_meta.get("key_file") or tls_meta.get("key") or "").strip()
            tls_domains_raw = tls_meta.get("domains")
            if isinstance(tls_domains_raw, str):
                tls_domains = [tls_domains_raw]
            elif isinstance(tls_domains_raw, Sequence):
                tls_domains = [str(value) for value in tls_domains_raw]
            else:
                tls_domains = []
            tls_domains = _dedupe_preserve_order(tls_domains or hostnames)
            if cert_file and key_file:
                cert_path = (self.certs_dir / cert_file).resolve()
                key_path = (self.certs_dir / key_file).resolve()
                if not cert_path.exists() or not key_path.exists():
                    missing = []
                    if not cert_path.exists():
                        missing.append(str(cert_path))
                    if not key_path.exists():
                        missing.append(str(key_path))
                    warnings.append(
                        f"{record.slug}: TLS files missing ({', '.join(missing)}); router will run without TLS"
                    )
                else:
                    tls_config = RouterTLSConfig(
                        cert_file=cert_file,
                        key_file=key_file,
                        domains=tls_domains,
                    )
        return (
            RouterDefinition(
                router_name=router_name,
                service_name=service_name,
                hostnames=hostnames,
                entrypoints=entrypoints,
                path_prefix=path_prefix,
                strip_prefix=strip_prefix,
                server_urls=server_urls,
                pass_host_header=pass_host_header,
                middleware_names=middleware_names,
                tls=tls_config,
            ),
            warnings,
        )


class TraefikConfigManager:
    """Continuously regenerates the Traefik config when the environment registry changes."""

    def __init__(
        self,
        store: EnvironmentStore,
        *,
        config_path: Path,
        certs_dir: Path,
        certs_mount_path: str = "/etc/traefik/certs",
        logger: Optional[logging.Logger] = None,
        poll_interval: float = 5.0,
    ) -> None:
        self.store = store
        self.config_path = config_path
        self.logger = logger or logging.getLogger(__name__)
        self.poll_interval = poll_interval
        self.builder = RouterConfigBuilder(certs_dir=certs_dir, certs_mount_path=certs_mount_path)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._watch_loop, name="TraefikConfigManager", daemon=True)
        self._last_revision = -1
        self._last_checksum: Optional[str] = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5)

    def synchronize(self, *, force: bool = False) -> bool:
        """Regenerate the config immediately."""
        records = self.store.list_environments()
        config, warnings = self.builder.build(records)
        for message in warnings:
            self.logger.warning(message)
        serialized = yaml.safe_dump(config, sort_keys=False)
        checksum = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if not force and checksum == self._last_checksum:
            return False
        self._write_atomic(serialized)
        self._last_checksum = checksum
        self._last_revision = self.store.get_revision()
        router_count = len(config.get("http", {}).get("routers", {}))
        self.logger.info("Traefik config updated (%s routers)", router_count)
        return True

    def _watch_loop(self) -> None:
        try:
            self.synchronize(force=True)
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.error("Initial Traefik config generation failed: %s", exc)
        while not self._stop_event.wait(self.poll_interval):
            revision = self.store.get_revision()
            if revision == self._last_revision:
                continue
            try:
                self.synchronize()
            except Exception as exc:  # pragma: no cover - defensive
                self.logger.error("Traefik config update failed: %s", exc)

    def _write_atomic(self, payload: str) -> None:
        target_dir = self.config_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = target_dir / f".{self.config_path.name}.tmp"
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(self.config_path)
