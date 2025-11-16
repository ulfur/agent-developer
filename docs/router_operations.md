# Router operations

Phase 2.3 introduces a Traefik edge router that terminates HTTP/HTTPS for every environment defined in `data/environments.json`. The backend keeps Traefik’s dynamic config (`data/router/environments.yml`) up to date, but operators are still responsible for provisioning certificates, DNS, and router metadata. This document captures the manual steps.

## 1. Define router metadata
Each environment that should be routable must include a `metadata.router` object:

```json
"metadata": {
  "router": {
    "hostnames": ["env.nightshift.local"],
    "entrypoints": ["websecure"],
    "service_url": "http://pi-dev.nightshift.local:8080",
    "path_prefix": "/",
    "tls": {
      "cert_file": "env.nightshift.local.crt",
      "key_file": "env.nightshift.local.key",
      "domains": ["env.nightshift.local"]
    }
  }
}
```

- `hostnames` – one or more FQDNs that Traefik should match (`RouterConfigBuilder` falls back to `host.hostname` when omitted, but explicit hostnames keep routes predictable).
- `service_url` or `port` – either point directly at an upstream URL or reference a named port from the registry’s `ports` list. If both are missing the generator will skip the environment.
- `path_prefix` and `strip_prefix` – optionally scope an environment to `/foo` and strip that prefix before forwarding.
- `tls.cert_file` / `tls.key_file` – filenames inside `data/router/certs/`. The backend logs a warning (and skips TLS) if the files are absent.

See `docs/environment_registry.md` for the full metadata schema.

## 2. Prepare certificates
Certificates never live inside git. Operators should:

1. Request the certificate (Let’s Encrypt, Cloudflare, internal CA, etc.).
2. Copy the PEM files into `data/router/certs/<name>.crt` and `<name>.key` on the host that runs Nightshift/Traefik.
3. Ensure permissions limit access to root (e.g., `chmod 600`).
4. Reference the filenames from the environment’s `metadata.router.tls` block.

Traefik binds `data/router/certs` into `/etc/traefik/certs`, so filenames map directly. When certificates rotate, overwrite the files and the backend will generate a fresh config automatically. Capture each issuance/renewal in the ops runbook and, if needed, add a Human Task so other operators can track progress.

## 3. DNS updates
Traefik can only serve hostnames that resolve to the router host:

1. Decide on the hostname(s) per environment (usually `<env>.<project>.local`).
2. Update the relevant DNS zone so A/AAAA records point at the Traefik host (Raspberry Pi IP, EC2 public IP, etc.).
3. Document the change in `logs/progress.log` (especially for new records) and create a Human Task whenever operator action is required (for example, waiting on network/dns team updates).

If an environment is still `planned` (no DNS yet), keep the router metadata in place so the config is ready—Traefik simply won’t see traffic until DNS is live.

## 4. Validate

- `python scripts/router_config.py --check-only --strict` (warn/error on metadata issues).
- `scripts/nightshift_compose.sh smoke` (runs the same validation + container self-checks).
- `docker compose logs router` (Traefik logs will point out malformed rules or missing files).
- `/api/health` (router warnings bubble up via `logs/progress.log` when the backend regenerates the config).

## 5. Tracking manual work
Use `scripts/human_tasks.py add ...` whenever certificate requests or DNS updates are pending. Each router-related Human Task should mention the environment id/slug, certificate thumbprint once issued, and any blockers (waiting on IT, pending CSR approval, etc.). Update or resolve the tasks as soon as the certs/DNS go live so the UI accurately reflects outstanding work.
