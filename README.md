<table align="center" border="0" cellspacing="0" cellpadding="0" width="100%">
  <tr>
    <td valign="middle" width="64px">
      <img src="frontend/nightshift-header-dark.png" alt="Nightshift logo" width="64" height="64">
    </td>
    <td valign="middle" >
      <strong style="font-size:26px; line-height:1;">Nightshift</strong><br>
      <span style="font-size:14px; color:#6c6c6c;">all day every day</span>
    </td>
  </tr>
</table>

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
- Every device/cloud instance pairs with `nghtshft.ai` on first boot: the backend prints a one-time pairing code (now surfaced in the login view, queue header, and `logs/progress.log`), the operator claims it in the control-plane UI, and the server writes `config/agent_identity.yml` plus the Cloudflare tunnel credentials it receives back from `POST /register-agent`. Never edit this file by hand—re-run the pairing flow if something changes.
- The identity blob includes Agent ID, friendly name, allowed repos/workspaces, PM tool tokens, Cloudflare hostname (`<agent>.nghtshft.ai`), and preferred ModelDriver. Agents refuse to process prompts until the file exists and the tunnel heartbeat passes, and operators can see the resolved metadata (hostname, agent ID, control-plane status) via the queue header chip, the workspace directory banner, and the `/api/agent/identity` endpoint.
- Monday.com (Jira/Linear coming soon) is the first PM integration: the daemon/webhook maps board items to prompts, updates status/comments as Nightshift progresses, and synchronizes Human Tasks both directions so blockers remain visible to operators regardless of the UI they use.
- Operators manage agents (pause/resume, prompt replay, pairing) through the control-plane dashboard; when offline, the device falls back to LAN-only mode but logs the degraded state in `logs/progress.log`.
- To refresh credentials manually, sign in and `POST /api/agent/identity/sync` (or click **Refresh identity** in the workspace panel). The backend automatically fetches `GET /agent/:id/config` on startup and records sync failures as pairing reminders in both the UI and `logs/progress.log`.

### Pairing workflow & smoke tests
Use these CLI checks to exercise the headless pairing path before exposing a device to operators:

1. Start `python3 backend/server.py` and watch `logs/progress.log` for `Awaiting control-plane pairing` entries plus the one-time code. The login card also shows the same code until pairing completes.
2. Confirm the pairing payload via `curl http://127.0.0.1:8080/api/agent/identity` (returns `{"status":"pairing","pairing_code":...}`) and ensure the queue header chip reflects the degraded state.
3. Simulate the control plane claiming the device by posting a bundle to `POST http://127.0.0.1:8080/register-agent` (see `config/agent_identity.yml` for the canonical schema). On success the backend writes the file, logs the pairing event, and the UI flips to the resolved agent metadata.
4. Verify the persisted identity with `curl http://127.0.0.1:8080/api/agent/identity` (status `paired`), inspect `config/agent_identity.yml`, and confirm the queue header chip will show the new name/hostname by hitting `curl -H "Authorization: Bearer <session>" http://127.0.0.1:8080/api/health` (the frontend uses the `identity` block from that payload).
5. Trigger a remote config refresh via `curl -X POST -H "Authorization: Bearer <session>" http://127.0.0.1:8080/api/agent/identity/sync` (or the **Refresh identity** UI button) so the new bundle exercises `GET /agent/:id/config`. Tail `logs/progress.log` for the `Control plane config refreshed` line plus `E-ink sections refreshed: footer_left/footer_right` to confirm the aux display footer picked up the paired status (run `./scripts/eink_section_selftest.py` if you need to force a footer refresh).
6. Keep the `human-task-control-plane-credential-bundle` entry in `data/human_tasks.json` updated—operators working that blocker supply the Cloudflare tunnel certs, nghtshft.ai API tokens, and Monday sandbox secrets that the pairing flow expects. If those secrets drift, queue a follow-up Human Task (and verify it lands in both the JSON store and `logs/progress.log`).

### Cloudflare tunnel health & LAN overrides
- Docker installs now include a `cloudflared` sidecar (see `docker-compose.yml`) that mounts `config/cloudflared/` and publishes the readiness endpoint on `http://cloudflared:43100/ready`. The backend container sets `TUNNEL_READY_URL=http://cloudflared:43100/ready` so `/api` stays guarded until the tunnel shows at least one ready connection.
- Bare-metal/systemd deployments should copy `systemd/cloudflared.service` and `systemd/nightshift.service` into `~/.config/systemd/user/`, install the `cloudflared` binary (the CDK user data now downloads it on EC2), and drop the issued bundle under `config/cloudflared/` (see `config/cloudflared/README.md`). The systemd unit uses `ConditionPathExists` so it waits for `config.yml` before starting.
- `/api/health`, the queue header, and the workspace banner now surface the tunnel status. When the readiness probe fails, the UI shows a blocking overlay and every `/api/*` request (except `/api/login`, `/api/agent/identity`, and `/api/health`) responds with HTTP 503 until the heartbeat recovers.
- LAN-only mode is for break-glass scenarios. Either export `ALLOW_LAN_MODE=1` or create `config/lan_mode_override` (override the path via `LAN_MODE_OVERRIDE_PATH`) once operations explicitly authorizes the bypass. The overlay reminds you which path to touch and the queue header chip shifts to “LAN mode” while the override is active.

| Variable | Default | Notes |
| --- | --- | --- |
| `REQUIRE_TUNNEL_HEALTH` | `1` | Disable only when bring-up scripts need to run without Cloudflare. |
| `TUNNEL_READY_URL` | `http://127.0.0.1:43100/ready` | Backend health probe. Compose overrides this to `http://cloudflared:43100/ready`. |
| `ALLOW_LAN_MODE` | `0` | Skip the tunnel block entirely (use with care). |
| `LAN_MODE_OVERRIDE_PATH` | `config/lan_mode_override` | Touch/remove this file to toggle LAN mode without editing env vars. |

Troubleshooting checklist:
1. `curl -sfS <ready-url>` – returns HTTP 200 with `readyConnections > 0` when the daemon is healthy.
2. `docker compose logs cloudflared` or `journalctl --user -u cloudflared.service -f` – watch for credential/DNS errors.
3. Verify `config/cloudflared/config.yml` points at the correct hostname and ingress target (`backend:8080` for Compose, `127.0.0.1:8080` on host installs) and that the issued `<tunnel-id>.json` exists with restrictive permissions.
4. Record every override in `logs/progress.log` and remove `config/lan_mode_override` as soon as the tunnel heartbeat returns.

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
  - See `docs/eink_display.md` for section layouts, refresh cadences, and UPS telemetry notes.
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

## Container Capabilities
- Backend images default to a `cloud` capability set so they're safe to build on laptops/VMs without GPIO headers. No hardware-only packages are installed in this mode.
- To layer in hardware support (E-ink/GPIO), set `NIGHTSHIFT_CAPABILITIES` to a comma-separated list (e.g., `cloud,gpio` or `cloud,eink`) before running `scripts/nightshift_compose.sh build|up`. Compose forwards the env var as a build arg, which unlocks the optional `python3-lgpio` dependency when the base OS provides it.
- Hardware-specific env vars (all `EINK_*` plus `ENABLE_EINK_DISPLAY`) still control runtime behaviour; the capability flag only changes what the container image tries to install. This keeps CI/cloud builds lightweight while allowing Pi deployments to opt into the extra drivers intentionally.

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

### UPS Telemetry (Geekworm X1201)
4. On the Pi, export `ENABLE_EINK_DISPLAY=1 ENABLE_UPS_TELEMETRY=1` (the telemetry flag defaults to `1`) before starting `backend/server.py` so the worker brings up the `X1201PowerMonitor`.
5. Tail `logs/progress.log` during boot; you should see either `UPS telemetry unavailable` diagnostics when I²C/GPIO are misconfigured or regular e-ink refresh entries once the fuel gauge at `0x36` is readable. Use `i2cdetect -y <bus>` if the Maxim gauge does not appear.
6. Pull the AC adapter briefly to confirm the aux display flips between `UPS: 93% 4.05V` + `Power: Charging from AC` and `UPS (LOW): …` + `Power: On battery backup`. The renderer automatically falls back to queue stats whenever telemetry drops out, so log warnings are your cue to adjust wiring or permissions.

| Variable | Default | Description |
| --- | --- | --- |
| `ENABLE_UPS_TELEMETRY` | `1` | Toggle Geekworm X1201 fuel-gauge + AC sensing.
| `UPS_I2C_BUS` | `1` | `/dev/i2c-*` index hosting the Maxim fuel gauge (address `0x36`).
| `UPS_I2C_ADDRESS` | `0x36` | Override if the HAT is reprogrammed.
| `UPS_AC_PIN` | `6` | BCM pin for the PLD/adapter-fault line (set `-1` to disable GPIO reads).
| `UPS_GPIO_CHIP` | `gpiochip0` | gpiod chip used to read `UPS_AC_PIN` (accepts numeric index or name).

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
