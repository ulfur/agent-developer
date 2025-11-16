# Environment registry

Phase 2.2 adds a persistent catalog (`data/environments.json`) so operators can see every dev/test host Nightshift touches without spelunking through README footnotes. Each record points back to the workspace spec stored at `projects/<id>/project.json`, which means we can always derive the source repositories, contacts, env vars, and smoke tests that apply to a given environment. The backend refuses to create/update an environment unless the referenced workspace exists in the registry.

## Schema
`backend/environments.py` owns the canonical schema. Every object contains:

| Field | Required | Notes |
| --- | --- | --- |
| `environment_id` | auto | Stable identifier (defaults to `env-<slug>-<hash>`). API paths use this value. |
| `project_id` | ✅ | Must match a `projects/<id>/project.json` entry so downstream tooling can pull the workspace spec + contacts. |
| `slug` | ✅ | Human friendly identifier (lowercase, `a-z0-9-`). Used by the CLI and future URLs. |
| `name`/`description` | ✅ / optional | Display metadata for the UI and CLI. |
| `host` | ✅ | Dict with `hostname`, `provider`, `region`, `ip`, and optional `notes`. Captures where the stack actually lives. |
| `ports` | optional | List of `{name, port, protocol, url, description}` objects describing exposed services. |
| `owner` | ✅ | Mirrors the workspace spec contacts (`name`, `email`, `slack`, `role`). Keeps escalation paths in sync. |
| `lifecycle` | ✅ | Dict with `state` (`planned`, `active`, `maintenance`, `retired`), `notes`, and `changed_at`. |
| `health` | ✅ | Dict with `status` (`unknown`, `healthy`, `degraded`, `maintenance`, `offline`), `url`, `checked_at`, `notes`. Used for `/api/health` aggregation. |
| `metadata` | optional | Arbitrary key/value tags (DNS zones, Traefik router names, credential hints, etc.). |
| `created_at` / `updated_at` | auto | UTC timestamps filled by the store for auditing. |

All write operations go through `EnvironmentStore`, so validation rules are enforced consistently whether the user edits JSON, calls `/api/environments`, or uses the CLI.

## Workspace spec linkage
Environment entries deliberately mirror portions of `docs/project_spec_schema.md`:

- `project_id` ensures we only create environments for workspaces with a valid spec. The API automatically hydrates the associated `ProjectDefinition` so frontends can display the scope/contact data that already lives beside the repo.
- `owner` defaults to a contact from the underlying workspace spec. When owners change, operators should update both the workspace spec *and* the environment entry so the registry stays in sync.
- `metadata` is the place to call out secrets managed outside the repo (for example, `traefik_router` names, DNS zones, or credential vault references). Those notes feed directly into follow-up Human Tasks when manual work is required.

By wiring everything back to the workspace specs we avoid duplicating runtime/toolchain knowledge—the registry is purely about *where* those specs run.

## API + CLI usage
The backend exposes full CRUD endpoints at `/api/environments` and folds aggregate stats into `/api/health.metrics.environments`, making it easy to watch for stale health checks or see how many planned environments still lack DNS.

For day-to-day edits, use `scripts/environments.py`:

```bash
# Inspect a single entry (slug or env id)
./scripts/environments.py show nightshift-dev

# Update health status / notes after a smoke test
./scripts/environments.py update nightshift-dev --health-status healthy --health-notes "Self-test OK"

# Remove an entry that was decommissioned
./scripts/environments.py delete env-nightshift-dev
```

The helper shares the same auth/env vars as the human-task and enqueue helpers (`AGENT_API_URL`, `AGENT_EMAIL`, `AGENT_PASSWORD`, `AGENT_TOKEN`).

## Manual follow-ups
Environment definitions capture DNS zones, router names, and credential placeholders, but the actual provisioning (updating DNS, issuing TLS certs, rotating secrets) still happens outside the repo. Whenever an environment references a host that does not have DNS/TLS/credential plumbing yet, log a Human Task (and update `docs/upgrade_plan.json`) so operators know manual work remains.

## Router metadata
Phase 2.3 adds a `metadata.router` object so Traefik can expose each environment automatically. Supported fields:

| Field | Type | Notes |
| --- | --- | --- |
| `hostnames` | list[str] | Required. Hostnames that Traefik should match. Falls back to `host.hostname` when omitted. |
| `entrypoints` | list[str] | Optional. Defaults to `["websecure"]`. Use `web` for HTTP-only routes. |
| `service_url` / `server_urls` | str/list[str] | Preferred upstream URL(s). Must include `http://` or `https://`. |
| `port` | str | Name of a port from the `ports` array. Used when `service_url` is omitted. |
| `path_prefix` | str | Path prefix (defaults to `/`). |
| `strip_prefix` | bool | Whether Traefik should strip the prefix before proxying (defaults to `true` when `path_prefix != "/"`). |
| `middlewares` | list[str] | Existing Traefik middleware names to attach to the router. |
| `pass_host_header` | bool | Defaults to `true`. Set `false` if the upstream expects a different Host header. |
| `tls.cert_file` / `tls.key_file` | str | Filenames inside `data/router/certs`. When both exist, Traefik will load them automatically. |
| `tls.domains` | list[str] | Overrides the certificate’s domain SANs (defaults to `hostnames`). |

See `docs/router_operations.md` for the full operator workflow (certificates, DNS, and validation).
