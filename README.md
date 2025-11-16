<p align="center">
  <img src="frontend/nightshift-header-dark.svg" alt="Nightshift" width="280">
</p>

# Nightshift

Nightshift is an autonomous, multi-agent development environment that keeps a Raspberry Pi 5-friendly toolchain while remaining portable across Docker and cloud installs. The platform runs itself (backend + frontend), enforces per-prompt git hygiene, and exposes operators to the exact queue/human-task state Codex sees.

## Highlights
- **Agent-first workflow** – `/api/prompts`, `data/prompts.json`, and `logs/progress.log` stay in sync so any prompt or follow-up task can be replayed with full context.
- **Guardrailed workspaces** – every project ships a `scope.yml` plus project-specific `context.md`/`agents.md` so agents know which files are writable and where to log status updates.
- **Environment registry + router** – `data/environments.json`, Traefik, and the Vue dashboard keep projects/environments searchable while documenting DNS/TLS ownership.
- **Human-in-the-loop safety** – `data/human_tasks.json`, the Task Queue, and the e-ink display highlight open blockers that require operators.
- **Control plane + PM sync** – the nghtshft.ai control plane registers each device/cloud agent, provisions Cloudflare tunnels/DNS, and keeps Monday.com (future Jira/Linear) boards mirrored into prompts + Human Tasks.
- **Consistent branding + status cues** – the refreshed Nightshift header mark ships in `frontend/nightshift-header-*.svg`, and rotating subtitles now surface the same quips on the UI header and auxiliary display.

## Architecture at a Glance
- **Frontend** – Single-file Vue 3 + Vuetify SPA (`frontend/index.html`) served as static assets. The global header shows the Nightshift logo, subtitle rotation (definitions + timer live near `frontend/index.html:2178-2205` and `:4782-4805`), queue health chips, and the searchable Project/Environment directory.
- **Backend** – Python 3.11 server (`backend/server.py`) that serves the SPA, exposes REST/WebSocket APIs, runs prompts through Codex (`git_branching.py` enforces branch workflow), manages the router config writer, and hydrates the e-ink worker.
- **Storage** – JSON stores in `data/` (`prompts.json`, `human_tasks.json`, `environments.json`) plus generated router YAML under `data/router/`. Prompt transcripts live in `logs/prompt_<id>.log` with a rolling operational feed at `logs/progress.log`.

## Control Plane & Agent Identity
- Every device/cloud instance pairs with `nghtshft.ai` on first boot: the backend prints a one-time pairing code, the operator claims it in the control-plane UI, and the server writes `config/agent_identity.yml` plus the Cloudflare tunnel credentials it receives back from `POST /register-agent`.
- The identity blob includes Agent ID, friendly name, allowed repos/workspaces, PM tool tokens, Cloudflare hostname (`<agent>.nghtshft.ai`), and preferred ModelDriver. Agents refuse to process prompts until the file exists and the tunnel heartbeat passes.
- Monday.com (Jira/Linear coming soon) is the first PM integration: the daemon/webhook maps board items to prompts, updates status/comments as Nightshift progresses, and synchronizes Human Tasks both directions so blockers remain visible to operators regardless of the UI they use.
- Operators manage agents (pause/resume, prompt replay, pairing) through the control-plane dashboard; when offline, the device falls back to LAN-only mode but logs the degraded state in `logs/progress.log`.

## Quick Start
### Bare-metal (Pi OS Lite or Linux)
1. Install Python 3.11+, git, and Pillow (`sudo apt install python3.11 python3-pip git python3-pil`).
2. Optional: export `CODEX_CLI=/path/to/codex` (the backend adds `--skip-git-repo-check`).
3. Launch the backend:
   ```bash
   python3 backend/server.py
   ```
4. Browse to `http://<host>:8080` (nginx proxy) or hit `http://127.0.0.1` locally. Override the bind address via `AGENT_HOST` / `AGENT_PORT`.
5. Watch the startup logs for the pairing code, claim it in the nghtshft.ai dashboard, and wait for the control plane to push the Cloudflare tunnel + agent identity bundle before running prompts.

### Docker Compose (recommended)
1. Copy `docker/.env.example` to `.env` (ignored by git) and tweak the bind-mount paths/ports. Defaults mount `./workspaces` into both services and publish the nginx frontend on `8080`.
2. Ensure Docker Engine + Compose v2 exist on the host and that `workspaces/` is present (`mkdir -p workspaces`).
3. Use the helper:
   ```bash
   ./scripts/nightshift_compose.sh up        # build + start backend, frontend, Traefik
   ./scripts/nightshift_compose.sh logs      # tail all containers
   ./scripts/nightshift_compose.sh down      # stop/remove
   ./scripts/nightshift_compose.sh smoke     # lint compose + build images + run self-checks
   ```
   The helper exports `NIGHTSHIFT_WORKSPACES_HOST` and `NIGHTSHIFT_REPO_HOST_PATH` before invoking Compose so Pi and cloud installs share the same `/workspaces` layout.
4. Open `http://<host>:8080` (or `FRONTEND_HTTP_PORT`) for the dashboard. Compose keeps the backend private on the docker network while nginx proxies `/api` + `/ws`.
5. Complete the nghtshft.ai pairing flow and verify the Cloudflare tunnel health indicator in the dashboard before exposing the instance to remote operators.

## Agent Platforms
- **E-Ink Edition** – Raspberry Pi 5 with the IT8951 panel and UPS HAT. Ships with the auxiliary HUD enabled, so pairing codes and queue summaries appear on-screen even before the frontend starts.
- **Touchscreen Edition** – Pi 5/CM5 with a touch UI overlay, local log viewer, and optional voice wake words for quick commands (pause agent, mark blocker resolved, etc.).
- **Cloud Edition** – Containerized Nightshift that runs inside hosted infrastructure; uses the same nghtshft.ai identity flow but gets workspaces from persistent volumes or network shares.

Use Human Tasks to log hardware/power blockers per device so operators can service them without stalling the rest of the fleet.

## Git Discipline & Workspace Layout
- Prompt branches follow `nightshift/prompt-<prompt_id>-<slug>`, are always based on `dev`, and must be clean before Codex switches contexts. `git_branching.py` refuses to run if the repo is dirty.
- Run `scripts/git_branch_smoke.py` (optionally with `--execute`) if branch automation feels off; prompt `2f4b09815f1044b9a1bb800bb9360bca` is queued to verify this path, so capture console output in `logs/progress.log` whenever you investigate it.
- `/workspaces/nightshift` is the live repo bind-mount. Additional repos belong under `/workspaces/<project>` so agents and operators see the same paths whether they are inside containers or on the host.

## Queue, Human Tasks & Planning
- Prompts live in `data/prompts.json` and surface through `/api/prompts` + `queue_snapshot` WebSocket events. Record the prompt IDs you queue plus any observed `queue_position` anomalies; prompt `335722ec9e05482f943fa1cd1ab7f859` is digging into an apparent FILO regression.
- Every attempt must append a summary to `logs/progress.log` (intent, branch, verifications, follow-ups). The Vue dashboard reads the same log excerpts for the Queue + History panels.
- Human blockers belong in `data/human_tasks.json`; manage them via `./scripts/human_tasks.py add|list|resolve`. Blocking tasks surface on the Task Queue card and on the e-ink screen.
- Monday.com is the first PM integration. The daemon/webhook mirrors assigned items into prompts, echoes Nightshift status/comments back to Monday, and synchronizes Human Tasks both ways. When edits happen outside Nightshift, verify they synced before progressing the prompt.
- Long-term planning happens in `docs/upgrade_plan.json`. Draft new tasks there, then queue them with `scripts/plan_prompt_queue.py` or `scripts/enqueue_prompt.py`. When you add prompts manually, cite the IDs both in `logs/progress.log` and in the `ROADMAP.md` “Latest queue updates” block.

## Environment Registry & Router
- `data/environments.json` is the canonical source. The schema lives in `docs/environment_registry.md`, while `docs/project_spec_schema.md` defines the project metadata consumed by the registry cards.
- Use `scripts/environments.py` to avoid hand-editing JSON:
  ```bash
  ./scripts/environments.py list --project nightshift
  ./scripts/environments.py show nightshift-dev
  ./scripts/environments.py update nightshift-dev --health-status degraded --health-notes "Node exporter offline"
  ./scripts/environments.py create \
    --project nightshift --slug traefik-edge --name "Nightshift Traefik Edge" \
    --hostname edge.nightshift.local --owner-name "Ulfur K" --owner-slack "#nightshift-ops" \
    --state planned --health-status unknown --port https:443:https://edge.nightshift.local
  ```
- Traefik lives alongside the backend/frontend containers. `backend/router_config.py` rewrites `data/router/environments.yml` whenever the registry changes, while `docker/traefik/traefik.yml` carries static config. Supply certificates/DNS manually per `docs/router_operations.md` and the open Human Tasks (`env-task-dns-*`, `env-task-tls-*`).

## UI & Observability Notes
- **Header branding** – `frontend/nightshift-header-dark.svg` / `...-light.svg` carry the refreshed mark (see `docs/nightshift-logo-spec.md`). The header subtitle rotates through curated quotes every ~45 seconds, and the same helper feeds the e-ink header via `backend/eink/renderer.py`.
- **Workspace directory** – Phase 2.4 keeps the registry permanently visible next to the queue with lifecycle badges, health notes, and quick-launch buttons. Follow-ups (`upgrade-env-ui-polish`, `upgrade-env-permissions`) will add column toggles, pinning, exports, and scoped views.
- **Queue health** – `/api/health` now emits per-status counts, oldest queued/running IDs, and human-task stats. The dashboard badges warn when waits >60s or runs >10m; treat those as incidents.
- **Auxiliary display** – The IT8591/IT8951 worker mirrors the queue + human tasks. It refreshes whenever `data/prompts.json` or `data/human_tasks.json` changes so the Pi-mounted screen stays current without manual nudges.

## CLI Helpers & Automation
- `scripts/enqueue_prompt.py` queues ad-hoc work. Pass inline text or pipe from `stdin`; auth comes from `AGENT_EMAIL`/`AGENT_PASSWORD` or `AGENT_TOKEN`.
- `scripts/plan_prompt_queue.py --plan docs/upgrade_plan.json --count <n>` dequeues the next `<n>` pending plan entries and persists them through the HTTP API.
- `scripts/human_tasks.py` manages blocker entries and mirrors the UI controls.
- `scripts/git_branch_smoke.py` sanity-checks git cleanliness/branch automation.
- `scripts/nightshift_compose.sh smoke` is the gate for docker changes; `scripts/docker_backend_selfcheck.sh` runs under the hood.

## Auxiliary E-ink Display (7.8" IT8591/IT8951)
1. Enable SPI via `raspi-config`, then install dependencies:
   ```bash
   sudo apt update && sudo apt install python3-lgpio python3-pil
   ```
2. Wire the HAT (default BCM pins: RST=17, CS=8, BUSY=24) or override with env vars.
3. Start the backend with `ENABLE_EINK_DISPLAY=1 python3 backend/server.py` (or set the variable in Compose).

Key environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `ENABLE_EINK_DISPLAY` | `0` | Toggle the worker.
| `EINK_WIDTH` / `EINK_HEIGHT` | `1872` / `1404` | Override detected panel size.
| `EINK_SPI_DEVICE` / `EINK_SPI_CHANNEL` | `0` / `0` | Map to `/dev/spidev<device>.<channel>`.
| `EINK_SPI_HZ` | `24000000` | SPI clock.
| `EINK_GPIO_CHIP` | `0` | `/dev/gpiochipX` index for LGPIO.
| `EINK_RST_PIN`, `EINK_BUSY_PIN`, `EINK_CS_PIN` | `17`, `24`, `8` | BCM pin overrides.
| `EINK_VCOM_MV` | `1800` | VCOM voltage.
| `EINK_ROTATE` | `180` | Panel rotation (0/90/180/270).
| `CODEX_ENABLE_SEARCH` | `1` | Enables `--search` for Codex runs (sharing the same env table as the backend worker).

The renderer splits the canvas into human-task and prompt columns, shows queue counts, the rotating subtitle, and the most recent entries with timestamps. Refreshes happen after every queue mutation or on demand via the backend admin actions.

## Headless Wi-Fi Bootstrap
For emergency Pi deployments without Ethernet:
```bash
cd /home/ulfurk/nightshift
sudo scripts/configure_wifi.sh <SSID> <password> [country]
```
The helper backs up `/etc/wpa_supplicant/wpa_supplicant.conf`, writes the new network stanza, and nudges `wpa_supplicant`. Reboot if the interface fails to rejoin.

## Reference Docs
- `ROADMAP.md` – living local plan, untracked in git.
- `agents.md` – shared guardrails for every prompt (plus per-project overrides under `projects/<id>`).
- `docs/upgrade_plan.json` – prioritized backlog for prompts Nightshift should queue next.
- `docs/environment_registry.md` / `docs/project_spec_schema.md` – schemas for registry/project definitions.
- `docs/router_operations.md` – operator-facing Traefik + TLS guidance.
- `docs/nightshift-logo-spec.md` – full branding specs for the header + favicon assets that now appear in this README.
