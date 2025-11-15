# Shared Agent Guidance

These instructions apply to every prompt, regardless of which project is currently active.

## Multi-project guardrails
- The repo hosts multiple focus areas under `projects/`. Each prompt declares its focus, and the backend stitches that project’s `context.md` (plus its optional `agents.md`) together with this shared guidance.
- Treat the “Project focus” header as authoritative. Only edit files that clearly belong to that project; never assume the Agent Dev Host itself is in scope unless the prompt explicitly targets it.
- Project-specific sources can live outside `projects/` (e.g., `frontend/projects/<name>`, dedicated backend modules, or standalone docs). If you are unsure a file belongs to the current project, stop and verify before touching it.
- Per-project guidance overrides anything here. When a project provides its own `agents.md`, consider it the final word on writable surfaces and scope boundaries for that run.

## Getting oriented
- Start with the selected project’s own `context.md`/`agents.md` and only branch out to shared docs when they explicitly apply.
- When you are working on the Agent Dev Host (`agent-dev-host`), also review `the_project.txt` and the repo `README.md` so platform-wide expectations stay fresh.
- Keep project-specific facts inside that project’s folder so guidance stays scoped. If you find general expectations that apply to everyone, update this shared file instead.
- Summarize each attempt in `logs/progress.log`. Include the prompt intent, key edits, skipped verifications, and remaining questions so future operators inherit context.

## Execution hygiene
- Run the smallest useful verification for the code you modify (tests, linters, or smoke scripts). If a check cannot run, explain why in both the log and your user-facing response.
- Call out any required system tweaks (network access, dependency installs, or hardware operations) before executing them. The environment should remain Raspberry-Pi friendly.
- Keep changes incremental and reviewable. Avoid sweeping refactors unless directly requested, and preserve user edits already present in the working tree.

## Prompt lifecycle awareness
- Prompts flow through the backend queue (`queued` → `running` → `completed` / `failed` / `canceled`). Each run writes a `logs/prompt_<id>.log` file that the UI summarizes for later review.
- You can enqueue prompts outside the UI with `scripts/enqueue_prompt.py` (authenticate via env vars or interactive prompts). Retry past prompts with `POST /api/prompts/<id>/retry` when rerunning a fix.
- Leave log files intact so prior attempts remain auditable. Mention skipped tests or manual verifications inline with your changes.

## Keeping guidance current
- Update this shared file whenever cross-project workflows change (logging expectations, verification standards, sandbox considerations, etc.). Host- or project-specific procedures belong in that project’s guidance file.
