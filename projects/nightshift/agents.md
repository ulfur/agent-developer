# Nightshift Guidance

## Orientation
- Re-read `the_project.txt` and the root `README.md` before making host changes so you keep the long-term goals and architecture in mind.

## Platform snapshot
- Self-hosted multi-project workspace targeting a Raspberry Pi 5 (or any Python 3.11+ Linux host). One backend process (`python backend/server.py`) serves the Vue/Vuetify frontend, REST APIs, WebSockets, and static assets under `frontend/` + `projects/`.
- Prompts execute inside this repository via the Codex CLI. Output streams to the UI, persists under `logs/prompt_<id>.log`, and feeds the queue summaries.
- Prompts execute inside this repository via the Codex CLI with `--search` enabled, so take advantage of web search when the task requires research and cite any sources you rely on in your response/log.
- Keep this project self-operable: document meaningful changes in `agents.md`, `README.md`, or `projects/nightshift/context.md` so future operators inherit an accurate playbook.

## Container runtime
- Docker Compose is now the baseline runtime: `docker/backend.Dockerfile` + `docker/frontend.Dockerfile` build the backend and nginx proxy, and `docker-compose.yml` wires them together with a `/workspaces` volume.
- Run `./scripts/nightshift_compose.sh up|down|logs|smoke` to control the stack. The helper exports `NIGHTSHIFT_WORKSPACES_HOST` and `NIGHTSHIFT_REPO_HOST_PATH` before invoking `docker compose` so Pi and AWS installs share the same `/workspaces/nightshift` layout.
- `/workspaces/nightshift` must contain the git checkout (bind-mounted from the host). Additional project repos that prompts operate on also live under `/workspaces/<project>`, so populate them from the host or remote filesystem (EFS/NFS) before queueing work.
- After editing Dockerfiles or compose YAML, run `./scripts/nightshift_compose.sh smoke` to validate the compose config, rebuild images, and execute the backend/frontend self-checks that gate deployments.

## Architecture & services
### Backend (`backend/server.py`)
- Pure stdlib HTTP server with JSON APIs under `/api/*`, WebSocket connections at `/ws`, and static hosting for the SPA plus project demos.
- `ProjectRegistry` loads metadata (`project.json`) for each folder under `projects/`, including optional `context.md` and `agents.md`.
- `PromptStore` persists prompt state in `data/prompts.json`, appends execution metadata to per-prompt logs, and marks stale `running` prompts as failed when the service restarts.
- `PromptWorker` pulls queued prompt ids, invokes `CodexRunner`, streams stdout/stderr via `EventStreamer`, and can cancel/retry runs.
- `CodexRunner` shells out to `CODEX_CLI` (default `codex exec --skip-git-repo-check -`). Configure `CODEX_CLI`, `CODEX_SANDBOX`, `AGENT_HOST`, `AGENT_PORT`, and `DEFAULT_PROJECT_ID` in `~/.config/systemd/user/nightshift.env` (or via the shell) before restarting the service.
- `CodexRunner` shells out to `CODEX_CLI` (default `codex exec --skip-git-repo-check --search -`). Configure `CODEX_CLI`, `CODEX_SANDBOX`, `CODEX_ENABLE_SEARCH`, `AGENT_HOST`, `AGENT_PORT`, and `DEFAULT_PROJECT_ID` in `~/.config/systemd/user/nightshift.env` (or via the shell) before restarting the service.
- `AuthManager` issues short-lived JWTs based on `data/users.json` + `data/.auth_secret`. `SSHKeyManager` maintains an ed25519 keypair under `data/ssh/` and mirrors it to `~/.ssh/`.

### Frontend (`frontend/index.html`)
- Single-file Vue3 + Vuetify SPA loaded from CDN builds. Supports login, live queue updates, per-attempt threads, stdout/stderr streaming, retries, and a settings pane for passwords + SSH keys.
- The project selector stores the user’s choice in `localStorage` (`codex-active-project`) and passes it with new prompts so the backend can stitch the correct context.
- The “Projects” launcher opens a project’s `launch_url` (e.g., `/projects/<id>/index.html`) so auxiliary demos stay co-hosted with the agent tools.

### Data, logs & supervisors
- `data/` – prompt DB, auth state, SSH keys, and future persistence artifacts.
- `logs/` – `progress.log`, `prompt_<id>.log`, and backend stdout/stderr (`logs/backend.stdout.log` / `.stderr.log`). Stream via `journalctl --user -u nightshift.service -f` or `tail -f`.
- Systemd unit: `~/.config/systemd/user/nightshift.service` keeps the backend running. After editing the unit or env file, run `systemctl --user daemon-reload` and restart the service.

### Prompt lifecycle & queue tooling
- Prompts enter via `POST /api/prompts` (`{"prompt": "...", "project_id": "..."}`) after authentication. States: `queued` → `running` → `completed` / `failed` / `canceled`.
- Operators can edit queued prompts (`PUT /api/prompts/<id>`), delete them, or retry (`POST /api/prompts/<id>/retry`).
- `scripts/enqueue_prompt.py` is a CLI helper for enqueueing prompts or retries directly from SSH; it accepts env vars for host, auth credentials, and project id.
- Queue snapshots (`GET /api/prompts` or the `queue_snapshot` WebSocket payload) now return queued items FIFO by `enqueued_at`, running (including `server_restarting`) ordered by `started_at`, and terminal entries afterward. The `status_buckets` map annotates IDs/counts per status, and each active prompt exposes a `queue_position` so the UI badges mirror the true worker order.
- When a restart (backend service, Codex CLI, or host) is required, create a dedicated "restart" prompt instead of manually bouncing processes mid-run. This keeps the request in the queue, avoids interrupting active work, and documents why the restart is happening for the next agent.
- Before you actually restart the backend, mark the running restart prompt with the special status so it survives the reboot:
  - Grab the `prompt_id` from the UI and run:

    ```bash
    curl -X POST http://127.0.0.1:8080/api/prompts/<prompt_id>/server_restarting \
      -H "Authorization: Bearer $AGENT_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"summary": "Restarting backend to land new config", "requires_follow_up": false}'
    ```

  - Set `requires_follow_up` to `true` only if the job needs more work after the host restarts. When it’s `false`, the backend auto-completes the prompt the moment it boots back up. When it’s `true`, the prompt stays in the queue with the `server_restarting` badge so the next agent can finish the manual verification.

## Quality bar for host work
- Maintain reliability for prompt execution, realtime visibility, and documentation. The system must stay Pi-friendly, so minimize heavyweight dependencies.
- Any change that affects operator workflows (new logs, CLI flags, env vars, etc.) should be reflected in `README.md`, this guidance file, and `logs/progress.log`.
- Keep the host easy to operate headlessly: mind logging verbosity, make default ports configurable, and avoid breaking systemd integration.

## Cloud deployment constraints
- AWS credentials stay off the Pi. Human Task `6be11393e7604c5398c0ba8486a0a1d2` resolved with the decision that operators will pull the repo locally and run all `cdk bootstrap`/`cdk deploy` steps themselves (`logs/prompt_9d73d11aa551425d89367964af7a72a8.log`). When you add or change infrastructure code, document the exact commands/operators need in `README.md` or the relevant prompt, then create/refresh a Human Task so they know when to execute the deployment manually.
- Point operators at `scripts/cdk.sh` and `cdk/README.md` when you request a deployment; include the target instance name (`-c instance=<name>`) and whether they should run `bootstrap`, `synth`, or `deploy`.
