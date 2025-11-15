# Agent-Enabled Dev Host

This repo hosts a lightweight proof-of-concept for an agent-accessible development workspace. It is designed for a Raspberry Pi 5 (Pi OS Lite) but can run anywhere Python 3.11+ is available.

## Architecture
- **Frontend**: Single-page Vue3 + Vuetify app served as static assets from `frontend/index.html`. Uses CDN builds to avoid a Node toolchain on constrained hosts.
- **Backend**: Pure Python HTTP server (`backend/server.py`) that serves the frontend, exposes a prompt queue API, and runs queued prompts through a pluggable Codex runner.
- **Storage**: JSON file at `data/prompts.json` for prompt metadata, per-prompt log files in `logs/`, and a rolling operations log at `logs/progress.log`.

## Getting Started
1. Ensure Python 3.11+ is installed (`python3 --version`).
2. Optional: export `CODEX_CLI=/path/to/codex` to point at a real Codex binary. The backend automatically passes `--skip-git-repo-check` so it can run outside a Git repo.
3. Start the backend:
   ```bash
   python3 backend/server.py
   ```
4. Visit the frontend from another machine on the network at `http://<host>` (port 80 is reverse-proxied to the backend via nginx; websocket traffic is forwarded automatically). If you’re on the same host, `curl http://127.0.0.1` should return the HTML shell.

The backend binds to `0.0.0.0` by default; override `AGENT_HOST` and `AGENT_PORT` environment variables as needed.

## API Summary
- `GET /api/health` – queue observability payload (status counts, oldest queued/running prompts + timestamps, rolling wait/run stats).
- `GET /api/prompts` – list of prompts (newest first).
- `POST /api/prompts` – add a prompt (`{"prompt": "..."}`).
- `GET /api/prompts/<id>` – prompt details + execution log.
- `POST /api/prompts/<id>/retry` – manually requeue a non-running prompt.
- `PUT /api/prompts/<id>` – edit a queued prompt’s text before it runs (body: `{ "prompt": "..." }`).
- `GET /api/logs` – contents of `logs/progress.log` (for future UI wiring).

Responses are JSON and CORS-enabled, so you can script against them with other tools.

## Queue Health Metrics
`/api/health` now keeps operators in the loop even when they are off the UI. The payload includes:
- `metrics.status_counts` – total prompts in each lifecycle bucket (`queued`, `running`, `completed`, `failed`, `canceled`).
- `metrics.oldest.queued` / `.running` – the prompt ID, enqueue/start timestamp, and computed age for the oldest work in each phase.
- `metrics.durations` – rolling averages and maxima for wait/run durations measured over the last 50 prompt completions (`window`/`samples` clarify how much data backs each number).

The frontend “Queue Health” card mirrors the same data:
- Status chips track counts per state so you can spot build-ups at a glance.
- The “Oldest queued/running” tiles surface the prompt ID (prefixed with `#`) and how long it has been sitting untouched. If either tile freezes for more than a few minutes, the queue is effectively stuck—inspect that prompt’s log and unblock it.
- Average/max wait and run durations are rendered under “Wait duration” and “Run duration”. When the backend sees any of the last 50 runs wait longer than 60s for a worker, a `Slow queue` badge appears. When a prompt runs for 10+ minutes, the UI shows a `Long runs` badge. Both are strong signals that the worker is wedged or an edit is looping.

Because `/api/health` is broadcast over the WebSocket channel as well as the REST endpoint, you can watch for those badges programmatically. Paired with the prompt IDs in `metrics.oldest.*`, it becomes trivial to page an operator (or enqueue a cancel/retry prompt) before the entire queue stalls.

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
cat prompt.txt | ./scripts/enqueue_prompt.py --project agent-dev-host
```

Defaults come from `AGENT_HOST` (`127.0.0.1`), `AGENT_PORT` (`8080`), and
`DEFAULT_PROJECT_ID`. Override the base URL entirely with `AGENT_API_URL` or
`--url`, and set `AGENT_TOKEN` if you prefer to skip the login request.

### Manual retries
Failed prompts stay in the queue history. Use the **Retry Prompt** button in the UI (or `POST /api/prompts/<id>/retry`) to requeue them once you’ve addressed the underlying issue.

## Running under systemd (recommended)
The repo ships with a user-level systemd unit so the backend survives SSH disconnects and restarts. Files live under `~/.config/systemd/user/`:

- `agent-dev-host.service` – points `ExecStart` at `/usr/bin/python3 backend/server.py`, restarts on failure, and redirects stdout/stderr to `logs/backend.stdout.log` / `logs/backend.stderr.log`.
- `agent-dev-host.env` – central place to define `PATH`, `CODEX_CLI`, `CODEX_SANDBOX`, `ENABLE_EINK_DISPLAY=1`, and any `EINK_*` pin overrides.

Day-to-day commands:

```bash
# After editing the unit or env file
systemctl --user daemon-reload

# Control the service
systemctl --user start agent-dev-host.service
systemctl --user stop agent-dev-host.service
systemctl --user restart agent-dev-host.service
systemctl --user status agent-dev-host.service

# View logs
journalctl --user -u agent-dev-host.service -f
tail -f logs/backend.stdout.log
```

The unit is enabled already (`systemctl --user enable agent-dev-host.service`). To have it come up automatically on boot, run `sudo loginctl enable-linger ulfurk` once so your user session is kept alive.

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

## Project Scope Manifests
- Each project folder under `projects/` now includes a `scope.yml` that declares its writable surface.
  The manifest keys are `description`, `allow`, `deny`, and `log_only`, all encoded as simple YAML (or
  JSON). `allow` lists glob patterns that are in scope, `deny` overrides those globs for shared
  surfaces that must stay read-only, and `log_only` is reserved for append-only paths such as
  `logs/progress.log`.
- `backend/server.py` loads every manifest into the `ProjectRegistry`, surfaces the data via
  `GET /api/projects`, and appends a “Scope guardrail” block to each prompt context so Codex sees the
  explicit allow/deny lists alongside the project’s `context.md` / `agents.md` guidance.
- If a project has no manifest yet, the registry falls back to a conservative guardrail that only
  allows files inside that project’s folder. The guardrail text is marked as a fallback to remind
  operators to author a manifest before expanding the writable surface.
- Update the manifest whenever a project grows a new directory tree, needs to deny a previously
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
| `EINK_ROTATE` | `180` | Rotation in degrees (0/90/180/270). `180` flips the panel for upside-down mounting. |

When enabled, the screen shows the most recent tasks (status, snippet, and last update time) immediately after every run, in addition to a running pending-count indicator.

## Headless Wi-Fi configuration

For emergency network setup (e.g., deploying the Pi without Ethernet), use the included helper:

```bash
cd /home/ulfurk/devhost
sudo scripts/configure_wifi.sh <SSID> <password> [country]
```

The script backs up `/etc/wpa_supplicant/wpa_supplicant.conf`, writes the new credentials, and nudges `wpa_supplicant`. Reboot if the interface was down. Keep credentials safe—this script writes them in plain text, just like the stock Raspberry Pi bootstrap flow.
