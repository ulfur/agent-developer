# Agent Dev Host Guidance

## Orientation
- Re-read `the_project.txt` and the root `README.md` before making host changes so you keep the long-term goals and architecture in mind.

## Platform snapshot
- Self-hosted multi-project workspace targeting a Raspberry Pi 5 (or any Python 3.11+ Linux host). One backend process (`python backend/server.py`) serves the Vue/Vuetify frontend, REST APIs, WebSockets, and static assets under `frontend/` + `projects/`.
- Prompts execute inside this repository via the Codex CLI. Output streams to the UI, persists under `logs/prompt_<id>.log`, and feeds the queue summaries.
- Keep this project self-operable: document meaningful changes in `agents.md`, `README.md`, or `projects/agent-dev-host/context.md` so future operators inherit an accurate playbook.

## Architecture & services
### Backend (`backend/server.py`)
- Pure stdlib HTTP server with JSON APIs under `/api/*`, WebSocket connections at `/ws`, and static hosting for the SPA plus project demos.
- `ProjectRegistry` loads metadata (`project.json`) for each folder under `projects/`, including optional `context.md` and `agents.md`.
- `PromptStore` persists prompt state in `data/prompts.json`, appends execution metadata to per-prompt logs, and marks stale `running` prompts as failed when the service restarts.
- `PromptWorker` pulls queued prompt ids, invokes `CodexRunner`, streams stdout/stderr via `EventStreamer`, and can cancel/retry runs.
- `CodexRunner` shells out to `CODEX_CLI` (default `codex exec --skip-git-repo-check -`). Configure `CODEX_CLI`, `CODEX_SANDBOX`, `AGENT_HOST`, `AGENT_PORT`, and `DEFAULT_PROJECT_ID` in `~/.config/systemd/user/agent-dev-host.env` (or via the shell) before restarting the service.
- `AuthManager` issues short-lived JWTs based on `data/users.json` + `data/.auth_secret`. `SSHKeyManager` maintains an ed25519 keypair under `data/ssh/` and mirrors it to `~/.ssh/`.

### Frontend (`frontend/index.html`)
- Single-file Vue3 + Vuetify SPA loaded from CDN builds. Supports login, live queue updates, per-attempt threads, stdout/stderr streaming, retries, and a settings pane for passwords + SSH keys.
- The project selector stores the user’s choice in `localStorage` (`codex-active-project`) and passes it with new prompts so the backend can stitch the correct context.
- The “Projects” launcher opens `launch_url` entries (e.g., `/projects/accgam/index.html`) so auxiliary demos stay co-hosted with the agent tools.

### Data, logs & supervisors
- `data/` – prompt DB, auth state, SSH keys, and future persistence artifacts.
- `logs/` – `progress.log`, `prompt_<id>.log`, and backend stdout/stderr (`logs/backend.stdout.log` / `.stderr.log`). Stream via `journalctl --user -u agent-dev-host.service -f` or `tail -f`.
- Systemd unit: `~/.config/systemd/user/agent-dev-host.service` keeps the backend running. After editing the unit or env file, run `systemctl --user daemon-reload` and restart the service.

### Prompt lifecycle & queue tooling
- Prompts enter via `POST /api/prompts` (`{"prompt": "...", "project_id": "..."}`) after authentication. States: `queued` → `running` → `completed` / `failed` / `canceled`.
- Operators can edit queued prompts (`PUT /api/prompts/<id>`), delete them, or retry (`POST /api/prompts/<id>/retry`).
- `scripts/enqueue_prompt.py` is a CLI helper for enqueueing prompts or retries directly from SSH; it accepts env vars for host, auth credentials, and project id.

## Quality bar for host work
- Maintain reliability for prompt execution, realtime visibility, and documentation. The system must stay Pi-friendly, so minimize heavyweight dependencies.
- Any change that affects operator workflows (new logs, CLI flags, env vars, etc.) should be reflected in `README.md`, this guidance file, and `logs/progress.log`.
- Keep the host easy to operate headlessly: mind logging verbosity, make default ports configurable, and avoid breaking systemd integration.
