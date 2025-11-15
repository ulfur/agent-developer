# Agent Operating Instructions

## Platform Snapshot
- The system now runs as a self-hosted multi-project agent workspace on a Raspberry Pi 5 (or any Linux host with Python 3.11+). A single backend process (`python backend/server.py`) serves the Vue/Vuetify frontend, a REST API, a WebSocket stream, and any project-specific static assets under `frontend/`.
- Prompts can target different focus areas that live under `projects/`. Each project carries its own `context.md` that is stitched together with this file before Codex runs so work stays scoped.
- Every queued prompt executes inside this very repository via the Codex CLI. Output is streamed live to browsers, persisted to `logs/prompt_<id>.log`, and summarized back into the prompt queue.

## Architecture & Services
### Backend (`backend/server.py`)
- Standard-library HTTP server with JSON APIs under `/api/*`, WebSocket connections at `/ws`, and static hosting for `frontend/` + `projects/`.
- `ProjectRegistry` discovers project metadata (id, description, optional launch URL, default flag) from `projects/*/project.json`.
- `PromptStore` persists queue state in `data/prompts.json`, appends attempt metadata to log files, and survives restarts by marking in-flight prompts as failed.
- `PromptWorker` pulls queued prompt ids, invokes `CodexRunner`, emits updates through `EventStreamer`, and can cancel/retry runs when asked.
- `CodexRunner` shells out to `CODEX_CLI` (default `codex exec --skip-git-repo-check -`) and streams stdout/stderr chunks back to the UI. Set `CODEX_CLI`, `CODEX_SANDBOX`, `AGENT_HOST`, `AGENT_PORT`, and `DEFAULT_PROJECT_ID` in `~/.config/systemd/user/agent-dev-host.env` (or your shell) before restarting the service.
- Authentication is baked in through `AuthManager`. User records (`data/users.json`) and signing secrets (`data/.auth_secret`) let the server issue short-lived JWTs that are required for any `/api/*` request beyond `/api/projects` and for WebSocket auth.
- `SSHKeyManager` ensures an ed25519 pair under `data/ssh/` and mirrors it into `~/.ssh/` for shell access. The public portion is exposed over `/api/user/ssh_keys` so the Settings UI can render it.

### Frontend (`frontend/index.html`)
- Single-file Vue3 + Vuetify SPA loaded via CDN builds. It supports a login gate, live queue updates, per-prompt attempt threads, stdout/stderr streaming panes, and a settings view for SSH keys and password changes.
- Users choose the “focused project” via the floating project selector. The choice is saved in `localStorage` (`codex-active-project`) and attached to new prompts so context can be restored later.
- The Projects launcher in the top bar lists everything from `/api/projects` and opens their `launch_url` (e.g., `/projects/accgam/index.html`) in a new tab so auxiliary demos can ship alongside the agent host.

### Data, Logs & Supervisors
- `data/` – prompts DB, auth state, SSH keys, and any future persistence.
- `logs/` – `progress.log` (operational log), `prompt_<id>.log` (per prompt), `backend.stdout.log`/`backend.stderr.log` (systemd output). Tail with `journalctl --user -u agent-dev-host.service -f` or `tail -f logs/backend.stdout.log`.
- Systemd unit: `~/.config/systemd/user/agent-dev-host.service` keeps the backend alive. Reload with `systemctl --user daemon-reload` after editing the service or env file, then `systemctl --user restart agent-dev-host.service`.

## Workflow Expectations
1. Before acting, read `the_project.txt` for goals, `README.md` for architecture, and `projects/<focus>/context.md` for project-specific guardrails.
2. Note each prompt’s intent/actions/results in `logs/progress.log` (and allow the backend to continue writing per-prompt logs). Mention skipped tests or manual verifications inline.
3. Favor incremental, reviewable changes with fast verification (unit tests when available, smoke checks for the backend/frontend, or lint scripts). If you skip a check, state why in the prompt log and in your UI response.
4. Call out when network access, package installs, or hardware tweaks are required. Keep the Pi-friendly footprint in mind.
5. Failed prompts remain visible; retry them through the UI button or `POST /api/prompts/<id>/retry` after diagnosing the failure.
6. Update this file whenever the workflow, APIs, or tooling meaningfully change so future operators inherit an accurate playbook.

## Prompt Lifecycle & Queue Management
- Prompts are accepted via `POST /api/prompts` (`{"prompt": "...", "project_id": "…"}`) after authentication. They begin as `queued`, transition to `running`, then land on `completed`, `failed`, or `canceled`.
- Operators can queue prompts without the UI through `scripts/enqueue_prompt.py`, which logs in (or accepts `AGENT_TOKEN`) and POSTs to `/api/prompts`.
- Every attempt is appended to `logs/prompt_<id>.log` with sections for context, stdout, stderr, duration, and status. `build_prompt_payload` parses these logs so the UI can show attempt timelines.
- Text edits: `PUT /api/prompts/<id>` rewrites queued/finished prompts. Deletions go through `DELETE /api/prompts/<id>`. Manual retries hit `/api/prompts/<id>/retry`.
- Cancellation: `POST /api/prompts/<id>/cancel` stops a running attempt. Include `{"restart": true}` to requeue automatically once Codex confirms the cancel.
- The queue broadcast feeds the WebSocket UI (`queue_snapshot`, `prompt_update`, `prompt_deleted`, `prompt_stream`, `health`) so browsers rarely poll `/api`. When no socket is available, the UI falls back to REST reads.

## Project Focus & Context
- Each `projects/<id>/` directory must contain `project.json` (metadata) and `context.md` (additional instructions). Context is rendered as:
  - Project header (name + description),
  - Project-specific context markdown,
  - Shared guidance from this `agents.md`.
- `ProjectRegistry` automatically scans this tree on startup. Mark `{"default": true}` to set the default focus or export `DEFAULT_PROJECT_ID=<id>` to override globally.
- When Codex runs a prompt, it receives the selected project id and the combined context text. Keep each context file current with guardrails, dependencies, or live endpoints unique to that project.

## Authentication & Access Control
- Default credentials: `ulfurk@ulfurk.com` / `dehost#1` (created automatically). Run `POST /api/login` to receive `{token, user}`; pass `Authorization: Bearer <token>` on every API call (except `/api/projects`) and send the same token to `/ws` by emitting `{type: "auth", token}` immediately after the socket opens.
- Tokens expire after `AUTH_TOKEN_TTL` seconds (12 hours by default). Update the env var if you need shorter/longer sessions.
- Password changes go through `PUT /api/user/password` with `{"current_password": "...", "new_password": "..."}`. All auth state sits under `data/users.json` and `data/.auth_secret`.

## Tooling & Runtime Flags
- Python 3.13, OpenSSH utilities (`ssh-keygen`), and any POSIX tooling already available on the host.
- Custom endpoints:
  - REST: `/api/health`, `/api/projects`, `/api/prompts`, `/api/prompts/<id>`, `/api/prompts/<id>/retry`, `/api/prompts/<id>/cancel`, `/api/logs`, `/api/user/ssh_keys`, `/api/user/password`.
  - WebSocket: `/ws` with events described above.
- Scripts: `scripts/configure_wifi.sh` helps bootstrap headless Wi-Fi (writes `/etc/wpa_supplicant/wpa_supplicant.conf`).
- Hardware flags: set `ENABLE_EINK_DISPLAY=1` (plus `EINK_*` pins/rotation) to light up the Waveshare IT8591/IT8951 e-ink panel managed by `backend/eink/manager.py`.

## Logging & Observability
- `logs/progress.log` – append operator-visible milestones (server restarts, manual fixes, prompt retries). `configure_logging()` also writes backend INFO logs here.
- `logs/prompt_<id>.log` – authoritative execution transcript. Never delete these mid-run.
- `logs/backend.stdout.log` / `logs/backend.stderr.log` – mirrored from the systemd unit for quick tailing.
- Use `journalctl --user -u agent-dev-host.service -f` to monitor live backend output, especially when debugging WebSocket or Codex runner issues.

## SSH Key Management
- Keys live under `data/ssh/` (ed25519). `SSHKeyManager` enforces permissions, regenerates missing halves, copies them into `~/.ssh/`, and exposes metadata (type, filename, fingerprint, string) via the REST API for UI display.
- Treat the managed keypair as canonical. If you rotate or delete it manually, clear both `data/ssh/` and `~/.ssh/` before letting the backend regenerate the key. Permissions must remain `600` for private keys and `644` for public keys.

## Realtime & Streaming Details
- Web clients connect to `/ws`, immediately send `{type: "auth", token}` (using the login token), and stay subscribed to these events:
  - `queue_snapshot` – entire prompt list.
  - `prompt_update` – detailed payload for a single prompt (includes latest log text and stdout preview).
  - `prompt_deleted` – informs clients to drop a prompt.
  - `prompt_stream` – incremental stdout/stderr chunks from `CodexRunner`.
  - `health` – uptime + pending count heartbeat every 10 seconds.
- `CodexRunner` pushes empty `reset` events at the start of each stream and `done` once stdout/stderr close. Handle reconnects by listening for `auth_ok` followed by immediate queue/health replays.
- If the socket dies, the frontend automatically falls back to polling `/api/prompts` until it reconnects with exponential backoff.

## Response Style & Frontend UX Notes
- UI real estate is limited, so keep agent responses short, structured around reasoning, and reference files with `path:line` links rather than pasting long diffs. Mention any skipped tests or manual validations explicitly.
- When summarizing code changes, describe the “why” first, then point at the files touched (e.g., ``backend/server.py:575``) so humans can jump into the repo.
- The frontend renders stdout/stderr streams inline; avoid echoing large command output in your response—just reference the prompt log.

## Display Orientation & Hardware Hooks
- The IT8591/IT8951 e-ink status console is opt-in via `ENABLE_EINK_DISPLAY=1`. Configure pins/resolution through `EINK_WIDTH`, `EINK_HEIGHT`, `EINK_SPI_*`, `EINK_GPIO_CHIP`, `EINK_RST_PIN`, `EINK_BUSY_PIN`, `EINK_CS_PIN`, `EINK_VCOM_MV`, and `EINK_ROTATE` (0/90/180/270, defaults to 180° for upside-down mounting).
- After changing any env var or pinout, run `systemctl --user daemon-reload && systemctl --user restart agent-dev-host.service`. Use `schedule_display_refresh("<reason>")` to nudge the manager when you mutate queue state outside the worker.

## Future Enhancements & Backlog
1. Swap the placeholder Codex CLI for the production-ready binary and harden sandboxing defaults.
2. Move prompts/persistence off the JSON file and into SQLite or Redis so multiple agents/processes can coordinate safely.
3. Expand authentication beyond a single shared user (user creation, role-based permissions, token revocation, audit logging).
4. Add per-project build hooks/tests plus telemetry panels (CPU, memory, queue durations) to the frontend dashboard.
5. Layer in durable WebSocket session storage so reconnects can resume stream state without reloading the entire prompt log.
