# Nightshift

Nightshift (`nightshift.git`) is a lightweight proof-of-concept for an agent-accessible development workspace. It is designed for a Raspberry Pi 5 (Pi OS Lite) but can run anywhere Python 3.11+ is available.

## Architecture
- **Frontend**: Single-page Vue3 + Vuetify app served as static assets from `frontend/index.html`. Uses CDN builds to avoid a Node toolchain on constrained hosts, keeps the Task Queue/composer/chat thread pinned in the primary column, and tucks the workspace overview into a sticky collapsible panel (with mobile toggles for queue/thread/workspaces) so operators never scroll past status data to reach the queue.
- **Backend**: Pure Python HTTP server (`backend/server.py`) that serves the frontend, exposes a Task queue API, and runs queued prompts through a pluggable Codex runner.
- **Storage**: JSON files at `data/prompts.json` (prompt metadata) and `data/human_tasks.json` (Human Task blockers), per-prompt log files in `logs/`, and a rolling operations log at `logs/progress.log`.

## Getting Started
1. Ensure Python 3.11+ is installed (`python3 --version`).
2. Optional: export `CODEX_CLI=/path/to/codex` to point at a real Codex binary. The backend automatically passes `--skip-git-repo-check` so it can run outside a Git repo.
3. Start the backend:
   ```bash
   python3 backend/server.py
   ```
4. Visit the frontend from another machine on the network at `http://<host>` (port 80 is reverse-proxied to the backend via nginx; websocket traffic is forwarded automatically). If you’re on the same host, `curl http://127.0.0.1` should return the HTML shell.

The backend binds to `0.0.0.0` by default; override `AGENT_HOST` and `AGENT_PORT` environment variables as needed.

## Container Runtime
Nightshift now ships multi-stage Dockerfiles under `docker/`:

- `docker/backend.Dockerfile` creates the Python runtime (git, ssh-keygen, lgpio, Pillow) used by the API worker.
- `docker/frontend.Dockerfile` packages the static Vue/Vuetify app behind an nginx proxy that forwards `/api` + `/ws` to the backend service.

The containers always read/write the live repository under `/workspaces/nightshift`, so git discipline still applies and prompt runs persist across restarts. Operators bind-mount the repo and a `/workspaces` directory that carries additional workspace checkouts or shared volumes (EFS on AWS, an SSD on the Pi, etc.).

### docker-compose workflow
1. Copy `docker/.env.example` to `.env` (ignored by git) and adjust host paths/ports as needed. Defaults mount `./workspaces` into the containers and forward the nginx frontend to port 8080.
2. Ensure Docker Engine + Compose v2 are installed on the host (Pi or cloud instance) and that the `workspaces/` directory exists (`mkdir -p workspaces`).
3. Use the helper to manage the stack:
   ```bash
   ./scripts/nightshift_compose.sh up      # build + start backend + frontend
   ./scripts/nightshift_compose.sh logs    # follow both containers
   ./scripts/nightshift_compose.sh down    # stop and remove containers
   ```
   The script exports `NIGHTSHIFT_WORKSPACES_HOST` and `NIGHTSHIFT_REPO_HOST_PATH` before shelling out to `docker compose`, so local Pi and remote hosts follow the exact same `/workspaces` layout. Override those vars in `.env` or the shell when `/workspaces` lives on EFS or another disk.
4. Visit `http://<host>:8080` (or whatever `FRONTEND_HTTP_PORT` you set) for the UI. The nginx frontend proxies `/api` and `/ws`, so the backend stays private on the compose network.

### `/workspaces` layout + git discipline
- `/workspaces/nightshift` – bind-mounted Nightshift repo. The backend entrypoint refuses to start if this directory is missing so git checks stay reliable.
- `/workspaces/<workspace>` – reserved for prompt workspaces Nightshift clones/operates on. Compose binds the host `workspaces/` directory here so operators can seed other repos or point it at a remote filesystem (EFS, NAS).
- Because prompt runs mutate `/workspaces/nightshift`, keep running `git status` / `git log` from the host just like before—containerizing the runtime does **not** change the requirement to commit/merge from the repo itself.

### Smoke tests
`./scripts/nightshift_compose.sh smoke` verifies the compose file, builds both images, and runs lightweight self-checks (`scripts/docker_backend_selfcheck.sh` + the nginx asset probe). Run it after editing Dockerfiles or compose YAML; the command fails on the first error so CI/agents can treat it as a gating test.

### Enabling the e-ink display inside containers
The backend image preinstalls `python3-lgpio` and related dependencies. To drive the IT8591 HAT, set `ENABLE_EINK_DISPLAY=1` and expose the GPIO/SPI devices to the `backend` container (for example, add this to `docker-compose.override.yml` on the Pi):

```yaml
services:
  backend:
    devices:
      - /dev/gpiomem:/dev/gpiomem
      - /dev/spidev0.0:/dev/spidev0.0
```

Keep `ENABLE_EINK_DISPLAY=0` when running on hosts without the hardware—the backend automatically skips the worker in that case.

## Prompt Git Workflow
Every prompt now runs on its own branch cut from `dev` so the queue never edits `main` directly:
- When a prompt starts, the backend checks that the workspace is clean, switches to `dev`, and creates `nightshift/prompt-<prompt_id>-<slug>`.
- All edits from that prompt happen on that branch. Agents **must not** merge into `main`; merges back into `dev` only happen when operators explicitly ask for it.
- When the attempt finishes and the tree is clean (changes committed/merged), the backend switches back to `dev` and deletes the prompt branch locally. If files are still dirty, cleanup is blocked and the prompt is marked failed until the operator resolves the leftovers.

### Configuration knobs
Tune the workflow with env vars before starting the backend:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NIGHTSHIFT_GIT_BASE_BRANCH` | `dev` | Base branch for prompt work. |
| `NIGHTSHIFT_PROMPT_BRANCH_PREFIX` | `nightshift/prompt` | Prefix for the per-prompt branch names. |
| `NIGHTSHIFT_BRANCH_SLUG_WORDS` | `6` | Number of prompt words used to build the slug. |
| `NIGHTSHIFT_BRANCH_SLUG_CHARS` | `48` | Maximum slug length (clipped and kebab-cased). |
| `NIGHTSHIFT_PROMPT_BRANCH_CLEANUP` | `1` | Set to `0` to leave branches checked out after a run. |
| `NIGHTSHIFT_GIT_ALLOW_DIRTY` | `0` | Set to `1` only for debugging to skip the clean-tree preflight. |
| `NIGHTSHIFT_GIT_DRY_RUN` | `0` | Log mutating git commands without running them (useful with the smoke test). |
| `NIGHTSHIFT_DISABLE_BRANCH_DISCIPLINE` | `0` | Disable the entire workflow if a workspace truly cannot support it. |

### Runtime env vars
All other configuration is exposed via environment variables so operators never have to edit container images:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT_HOST` | `0.0.0.0` | Bind interface for the backend HTTP server. |
| `AGENT_PORT` | `8080` | Backend HTTP port (nginx/frontends typically proxy to it). |
| `DEFAULT_PROJECT_ID` | `nightshift` | Workspace ID pre-selected for prompts/UI (still passed as `project_id`). |
| `CODEX_CLI` | `codex` | Path to the Codex CLI binary invoked by the runner. |
| `CODEX_SANDBOX` | _(unset)_ | Optional sandbox flag forwarded to Codex (`--sandbox <mode>`). |
| `CODEX_ENABLE_SEARCH` | `1` | Set to `0` to run Codex without `--search`. |
| `ENABLE_EINK_DISPLAY` | `0` | Set to `1` to enable the IT8591 display worker. |
| `EINK_*` | see `docker-compose.yml` defaults | Pin configuration for the e-ink HAT (width/height, GPIO/SPI pins, rotation, etc.). |

For Docker users, `docker/.env.example` documents the host-side knobs (`FRONTEND_HTTP_PORT`, `NIGHTSHIFT_WORKSPACES_HOST`, `NIGHTSHIFT_REPO_HOST_PATH`). Copy it to `.env`, tweak as needed, and the helper script will inject them before calling Compose.

### Smoke test
Use `scripts/git_branch_smoke.py` to dry-run the workflow and verify your repo is ready:

```bash
# Preview the git commands without touching the tree
./scripts/git_branch_smoke.py

# Actually create and delete a test branch (use on clean dev checkouts only)
./scripts/git_branch_smoke.py --execute --prompt-id smoke123 --prompt "Validate branch discipline"
```

The script exits non-zero if the repo is dirty, the `dev` branch is missing, or cleanup cannot complete—treat that as a blocker before queueing prompts.

## API Summary
- `GET /api/health` – queue observability payload (status counts, oldest queued/running prompts + timestamps, rolling wait/run stats).
- `GET /api/prompts` – queue snapshot (queued FIFO, running ordered by `started_at`, terminal items appended) plus `status_buckets` + `queue_position` metadata (mirrors the `queue_snapshot` WebSocket payload).
- `POST /api/prompts` – add a prompt (`{"prompt": "..."}`).
- `GET /api/prompts/<id>` – prompt details + execution log.
- `POST /api/prompts/<id>/retry` – manually requeue a non-running prompt.
- `PUT /api/prompts/<id>` – edit a queued prompt’s text before it runs (body: `{ "prompt": "..." }`).
- `GET /api/environments` – list the environment registry (`?project_id=...` filter is optional).
- `POST /api/environments` – create a new entry (host/owner/ports/lifecycle/health metadata).
- `GET /api/environments/<id>` – fetch the normalized payload for a single environment (includes workspace context).
- `PUT /api/environments/<id>` – update any subset of fields (host, owner, ports, lifecycle, health, metadata).
- `DELETE /api/environments/<id>` – remove an entry when an environment is decommissioned or recreated elsewhere.
- `GET /api/logs` – contents of `logs/progress.log` (for future UI wiring).

Responses are JSON and CORS-enabled, so you can script against them with other tools.

## Queue Health Metrics
`/api/health` now keeps operators in the loop even when they are off the UI. The payload includes:
- `metrics.status_counts` – total prompts in each lifecycle bucket (`queued`, `running`, `completed`, `failed`, `canceled`).
- `metrics.oldest.queued` / `.running` – the prompt ID, enqueue/start timestamp, and computed age for the oldest work in each phase.
- `metrics.durations` – rolling averages and maxima for wait/run durations measured over the last 50 prompt completions (`window`/`samples` clarify how much data backs each number).
- `metrics.environments` – snapshot of the environment registry (total entries, status/lifecycle counts, and how many health checks are stale).

The frontend “Queue Health” card mirrors the same data:
- Status chips track counts per state so you can spot build-ups at a glance.
- The “Oldest queued/running” tiles surface the prompt ID (prefixed with `#`) and how long it has been sitting untouched. If either tile freezes for more than a few minutes, the queue is effectively stuck—inspect that prompt’s log and unblock it.
- Average/max wait and run durations are rendered under “Wait duration” and “Run duration”. When the backend sees any of the last 50 runs wait longer than 60s for a worker, a `Slow queue` badge appears. When a prompt runs for 10+ minutes, the UI shows a `Long runs` badge. Both are strong signals that the worker is wedged or an edit is looping.

Because `/api/health` is broadcast over the WebSocket channel as well as the REST endpoint, you can watch for those badges programmatically. Paired with the prompt IDs in `metrics.oldest.*`, it becomes trivial to page an operator (or enqueue a cancel/retry prompt) before the entire queue stalls.

## Human Tasks Queue
Section 0.2 of the roadmap is now live: the backend persists operator blockers in `data/human_tasks.json`,
surfaces them in the UI, and exposes the data alongside prompt metrics.

### API endpoints
- `GET /api/human_tasks` – list every task plus a summary (status counts, blocking count, oldest blocking task, revision).
- `POST /api/human_tasks` – create a new entry (`title`, optional `description`, workspace `project_id`, `prompt_id`, `blocking`, `status`).
- `PUT /api/human_tasks/<id>` – update any subset of the fields.
- `DELETE /api/human_tasks/<id>` – remove stray entries (resolved tasks should normally stay around for auditing).

`/api/health.metrics.human_tasks` mirrors the same summary and ships down every 10 seconds over the WebSocket health broadcast. The Vue dashboard listens for the revision number in that payload and refreshes the visible queue automatically when it changes.

### CLI helper
Use `scripts/human_tasks.py` to log blockers without touching the UI:

```bash
# Add a blocking task scoped to a workspace/prompt
./scripts/human_tasks.py add "Awaiting security review" \
  --project nightshift --prompt 87cdc9a0ffab4628a019ca681a942d41 \
  --blocking --description "Need sign-off before modifying prod config."

# List only blocking items (JSON or human-friendly output)
./scripts/human_tasks.py list --blocking-only
./scripts/human_tasks.py list --json | jq .

# Resolve or delete tasks
./scripts/human_tasks.py resolve <task_id>
./scripts/human_tasks.py delete <task_id>
```

The left column of the dashboard now includes a “Human Tasks” panel under the prompt queue. It highlights the number of blocking/open items, the latest updates, and the related project/prompt IDs so operators can spot stuck work quickly.

### Restart-aware prompts
- When a queued/running prompt is going to intentionally restart the backend or host, mark it with the special `server_restarting` status before you bounce services. This keeps the queue from flagging the prompt as failed while the backend restarts.
- Use the authenticated API to mark the active prompt:

```bash
curl -X POST http://127.0.0.1:8080/api/prompts/<prompt_id>/server_restarting \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"summary": "Restarting backend to pick up config changes", "requires_follow_up": false}'
```

Set `requires_follow_up` to `true` when the job needs another run after the host comes back (for example, when you plan to re-run validation commands manually). Otherwise leave it `false` and the backend will automatically mark the prompt as `completed` the next time it boots.  
- On startup the server inspects any `server_restarting` prompts. Those without follow-up requirements are completed automatically, while the rest stay visible in the queue so the next agent knows to finish the post-restart validation.

## Environment Registry & CLI
Phase 2.2 introduces a first-class registry under `data/environments.json`. Each entry links back to the workspace spec under `projects/<id>/project.json` so we always know which source repos, contacts, and runtime expectations apply to that environment. The schema is documented in `docs/environment_registry.md`, but in short every record includes:

- `project_id`/`slug`/`name` – how the environment ties back to the workspace registry.
- `host` – hostname/IP/provider/region + freeform notes about the host.
- `ports` – one or more named service bindings (protocol/port/url/description).
- `owner` – contact responsible for the environment (mirrors the workspace spec’s contacts).
- `lifecycle` – `planned`/`active`/`maintenance`/`retired` + operator notes.
- `health` – status + optional health URL/notes/`checked_at` timestamp.
- `metadata` – arbitrary tags (DNS zones, Traefik router names, secret management hints, etc.).

The backend exposes CRUD endpoints at `/api/environments` (documented above), hydrates workspace details automatically, and folds aggregate stats into `/api/health.metrics.environments`. Operators can manage definitions without editing JSON by using the new CLI:

```bash
# List everything or scope to a workspace
./scripts/environments.py list
./scripts/environments.py list --project nightshift

# Show or update a single host (slug or env id works)
./scripts/environments.py show nightshift-dev
./scripts/environments.py update nightshift-dev --health-status degraded --health-notes "Node exporter offline"

# Register a fresh environment
./scripts/environments.py create \\
  --project nightshift --slug traefik-edge --name "Nightshift Traefik Edge" \\
  --hostname edge.nightshift.local --owner-name "Ulfur K" --owner-slack "#nightshift-ops" \\
  --state planned --health-status unknown --port https:443:https://edge.nightshift.local
```

All CLI options honor `AGENT_*`/`DEFAULT_PROJECT_ID`/`AGENT_TOKEN` just like the other helpers, so you can script bulk updates from SSH while the Vue UI catches up later.

## Workspace Directory (UI)
Phase 2.4 upgrades the Vue/Vuetify dashboard so the registry data is always visible beside the queue. A single Workspaces card stays pinned next to the Task Queue:

- **Workspace list** – backfilled from `/api/projects`, searchable, and annotated with runtime/toolchain summaries, primary contacts, launch URLs, and human-task counts. Each entry now renders the linked environments inline, complete with lifecycle/health chips, blocking-task badges, host notes, and quick-launch buttons. Unscoped environments fall into their own “Unscoped workspaces” bucket so nothing gets lost.
- **Streaming + logging** – the card listens for the same `/api/health.metrics.environments` snapshot that the backend broadcasts every 10 seconds, so status chips and stale-health counters stay up-to-date without manual refreshes. A manual Refresh button is still available and logs the last sync timestamp.

Document your UX edits in this section whenever the cards change—operators treat it as the canonical guide for what the dashboard is supposed to show.

## Traefik Router & TLS
Phase 2.3 adds a Traefik edge router so every environment listed in the registry can be exposed via predictable hostnames + TLS. The backend now runs a `TraefikConfigManager` thread (`backend/router_config.py`) that watches the registry for changes, writes a dynamic config to `data/router/environments.yml`, and keeps Traefik in sync without a restart. Operators can regenerate the file manually (or validate metadata) with:

```bash
python scripts/router_config.py --stdout       # print the generated YAML
python scripts/router_config.py --check-only   # validate without writing
```

The router container lives alongside the backend/frontend inside docker-compose:

- Static config: `docker/traefik/traefik.yml`.
- Dynamic config + certs: `data/router/` (bind-mounted into `/etc/traefik/dynamic` and `/etc/traefik/certs`).
- Ports: `TRAEFIK_HTTP_PORT` (default 80) and `TRAEFIK_HTTPS_PORT` (default 443).
- Additional overrides: `TRAEFIK_STATIC_CONFIG`, `TRAEFIK_DYNAMIC_HOST_PATH`, `TRAEFIK_CERTS_HOST_PATH`.

When you run `scripts/nightshift_compose.sh up` the helper now creates the router directories automatically and `scripts/nightshift_compose.sh smoke` validates the generated config before Traefik starts. Operators are still responsible for supplying real certificates + DNS — see `docs/router_operations.md` for the manual workflow. Each environment that should be routed must declare a `metadata.router` block (hostnames, upstream port/URL, optional TLS files); the schema is documented in `docs/environment_registry.md`.

## CLI Prompt Helper
Queueing something quickly from SSH is often easier than opening the Vue app.
Use `scripts/enqueue_prompt.py` to log in (or reuse an existing token) and fire
a prompt at the backend:

```bash
# Option 1: pass the prompt inline
./scripts/enqueue_prompt.py "Check the latest deploy log" \
  --email ulfurk@ulfurk.com --password 'dehost#1'

# Option 2: pipe multi-line text and reuse env vars for auth/host config
export AGENT_EMAIL=ulfurk@ulfurk.com
export AGENT_PASSWORD='dehost#1'
cat prompt.txt | ./scripts/enqueue_prompt.py --project nightshift
```

Defaults come from `AGENT_HOST` (`127.0.0.1`), `AGENT_PORT` (`8080`), and
`DEFAULT_PROJECT_ID`. Override the base URL entirely with `AGENT_API_URL` or
`--url`, and set `AGENT_TOKEN` if you prefer to skip the login request.

### Manual retries
Failed prompts stay in the queue history. Use the **Retry Prompt** button in the UI (or `POST /api/prompts/<id>/retry`) to requeue them once you’ve addressed the underlying issue.

### Queue snapshot schema
`GET /api/prompts` and the `queue_snapshot` WebSocket event now return the same structured payload so the UI and CLI helpers can stay in sync:
- `items` – queued prompts arrive first (FIFO by `enqueued_at`), running prompts follow (ordered by `started_at`/`server_restart_marked_at`), and terminal entries appear last sorted by their most recent update. Each queued or running entry includes a `queue_position` value so badges/labels mirror the worker’s actual FIFO order; completed/failed/canceled items expose `queue_position: null`.
- `status_buckets` – per-status metadata (`queued`, `running`, `server_restarting`, `completed`, `failed`, `canceled`) listing prompt IDs in display order alongside a `count` so the frontend can render badges without recomputing the groupings.

The schema is backward compatible with existing consumers that expect `items` at the root—new fields can simply be ignored if they are not needed.

## Running under systemd (recommended)
The repo ships with a user-level systemd unit so the backend survives SSH disconnects and restarts. Files live under `~/.config/systemd/user/`:

- `nightshift.service` – points `ExecStart` at `/usr/bin/python3 backend/server.py`, restarts on failure, and redirects stdout/stderr to `logs/backend.stdout.log` / `logs/backend.stderr.log`.
- `nightshift.env` – central place to define `PATH`, `CODEX_CLI`, `CODEX_SANDBOX`, `ENABLE_EINK_DISPLAY=1`, and any `EINK_*` pin overrides.

Day-to-day commands:

```bash
# After editing the unit or env file
systemctl --user daemon-reload

# Control the service
systemctl --user start nightshift.service
systemctl --user stop nightshift.service
systemctl --user restart nightshift.service
systemctl --user status nightshift.service

# View logs
journalctl --user -u nightshift.service -f
tail -f logs/backend.stdout.log
```

The unit is enabled already (`systemctl --user enable nightshift.service`). To have it come up automatically on boot, run `sudo loginctl enable-linger ulfurk` once so your user session is kept alive.

## Codex Runner Stub
Until the real Codex CLI is available, the backend writes placeholder output to the prompt log. Once Codex is deployed on the device:
1. Install or copy the CLI into the PATH.
2. Export `CODEX_CLI` if the binary name differs from `codex`.
3. Restart the backend; each queued prompt will now call Codex with `codex --prompt "<text>"` within the repo root.

## Development Notes
- Keep `agents.md` current with guidance for future agents/collaborators.
- Extend persistence to a proper datastore before moving to production.
- Add auth + TLS before exposing outside a trusted LAN.
- For realtime UX, consider adding Server-Sent Events or WebSockets to broadcast prompt updates.

## AWS CDK Infrastructure
Phase 1.1 now ships a full AWS CDK (Python) application under `cdk/` that
stands up the Nightshift VPC, NAT, Auto Scaling group, EFS workspace, and S3 log
archive bucket. Operators run these commands on their own machines—Nightshift
never stores AWS credentials on the Pi host.

1. Install Node.js 18+, the AWS CDK CLI (`npm install -g aws-cdk`), and Python
   3.11 on your workstation. Inside `cdk/`, create/activate a virtualenv and
   install dependencies via `pip install -r requirements.txt`.
2. Copy `cdk/instances/example-dev.yml` to `cdk/instances/<instance>.yml`, fill
   in the AWS account/region/tags, and update the `parameters` map (CIDRs,
   compute AMI + instance type, disk/min/max capacity, SSH CIDRs, etc.).
3. Run commands from the repo root with the helper script:
   - `scripts/cdk.sh synth -c instance=<name>` validates the template.
   - `scripts/cdk.sh diff -c instance=<name>` previews changes.
   - `scripts/cdk.sh deploy Nightshift-<slug> -c instance=<name>` applies them.
   The helper falls back to `npx cdk` if no standalone `cdk` binary is present.
4. Ensure your IAM principal can create/modify VPCs, subnets, route tables, NAT
   gateways, security groups, Elastic IPs, Auto Scaling groups, launch
   templates, S3 buckets, EFS file systems/access points, and the IAM instance
   profiles the ASG needs (`ec2:*`, `autoscaling:*`, `elasticfilesystem:*`,
   `s3:*`, `iam:PassRole` covers the required APIs).
5. Every time new CDK code lands (or after you update an instance config), log
   a Human Task that references the exact command (for example: “Run
   `scripts/cdk.sh deploy Nightshift-prod -c instance=prod` from your AWS
   workstation”). Operators close the task after manually running `cdk
   bootstrap`/`deploy`.

See `cdk/README.md` for the full list of stack resources and parameter details.

## Workspace Scope Manifests
- Each workspace folder under `projects/` now includes a `scope.yml` that declares its writable surface.
  The manifest keys are `description`, `allow`, `deny`, and `log_only`, all encoded as simple YAML (or
  JSON). `allow` lists glob patterns that are in scope, `deny` overrides those globs for shared
  surfaces that must stay read-only, and `log_only` is reserved for append-only paths such as
  `logs/progress.log`.
- `backend/server.py` loads every manifest into the `ProjectRegistry`, surfaces the data via
  `GET /api/projects`, and appends a “Scope guardrail” block to each prompt context so Codex sees the
  explicit allow/deny lists alongside the workspace’s `context.md` / `agents.md` guidance.
- If a workspace has no manifest yet, the registry falls back to a conservative guardrail that only
  allows files inside that workspace’s folder. The guardrail text is marked as a fallback to remind
  operators to author a manifest before expanding the writable surface.
- Update the manifest whenever a workspace grows a new directory tree, needs to deny a previously
  writable area, or wants to clarify which logs are append-only. The runtime guard described in
  `docs/project_scope_enforcement.md` will consume these globs once enforcement lands, so accuracy
  matters even today.

## Optional: 7.8" IT8591/IT8951 E‑Ink Status Display
The backend can mirror the latest queue activity on a Waveshare 7.8" e‑ink HAT (IT8591/IT8951 controller) attached to a Raspberry Pi 5 via the LGPIO stack. The update path runs in a dedicated thread so prompt execution never blocks on display refreshes.

1. Enable SPI in `raspi-config`, then install the userland dependencies:
   ```bash
   sudo apt update
   sudo apt install python3-lgpio python3-pil
   ```
2. Wire the HAT using the default pins (RST=17, CS=8, BUSY=24) or export custom BCM numbers via the env vars below.
3. Configure and start the backend with:
   ```bash
   ENABLE_EINK_DISPLAY=1 python3 backend/server.py
   ```

Environment variables (all optional apart from `ENABLE_EINK_DISPLAY`):

| Variable | Default | Description |
| --- | --- | --- |
| `ENABLE_EINK_DISPLAY` | `0` | Switch the display integration on/off. |
| `EINK_WIDTH` / `EINK_HEIGHT` | `1872` / `1404` | Override the detected panel resolution. |
| `EINK_SPI_DEVICE` / `EINK_SPI_CHANNEL` | `0` / `0` | Map to `/dev/spidev<device>.<channel>`. |
| `EINK_SPI_HZ` | `24000000` | SPI clock in Hz for the IT8591 controller. |
| `EINK_GPIO_CHIP` | `0` | GPIO chip index or `/dev/gpiochipX` path for LGPIO. |
| `EINK_RST_PIN`, `EINK_BUSY_PIN`, `EINK_CS_PIN` | `17`, `24`, `8` | BCM pins for reset/busy/chip-select. |
| `EINK_VCOM_MV` | `1800` | VCOM (mV) applied to the panel. Adjust per display. |
| `EINK_ROTATE` | `180` | Display rotation (0/90/180/270). Defaults to 180° for upside-down mounting. |
| `CODEX_ENABLE_SEARCH` | `1` | Append `--search` when launching Codex (set to `0` to disable). |
| `EINK_ROTATE` | `180` | Rotation in degrees (0/90/180/270). `180` flips the panel for upside-down mounting. |

When enabled, the screen shows the most recent tasks (status, snippet, and last update time) immediately after every run, in addition to a running pending-count indicator.

## Headless Wi-Fi configuration

For emergency network setup (e.g., deploying the Pi without Ethernet), use the included helper:

```bash
cd /home/ulfurk/nightshift
sudo scripts/configure_wifi.sh <SSID> <password> [country]
```

The script backs up `/etc/wpa_supplicant/wpa_supplicant.conf`, writes the new credentials, and nudges `wpa_supplicant`. Reboot if the interface was down. Keep credentials safe—this script writes them in plain text, just like the stock Raspberry Pi bootstrap flow.
