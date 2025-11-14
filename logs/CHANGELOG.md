# Change Log

## 2025-11-14
- Backend now broadcasts queue, prompt, and health updates over `/ws`, with the Vue dashboard consuming realtime pushes instead of polling.
- The frontend exposes an Operations Log panel that streams `/api/logs`, auto-refreshes every 30s, and surfaces error states plus manual refresh.

## 2025-11-13
- Initial backend queue server (`backend/server.py`) with REST API and Codex runner stub.
- Static Vue3 + Vuetify frontend (`frontend/index.html`) for prompt submission and monitoring.
- Added `agents.md`, JSON persistence (`data/prompts.json`), and logging scaffolding under `logs/`.
- Added manual retry endpoint/UI button plus CLI invocation fixes (`--skip-git-repo-check`).
- Added user-level systemd unit (`agent-dev-host.service`) and env file for Codex/e-ink configuration; logs now redirect to `logs/backend.stdout.log` / `backend.stderr.log`.
