# Shared Agent Guidance

These instructions apply to every prompt, regardless of which project is currently active.

## Multi-project guardrails
- The repo hosts multiple focus areas under `projects/`. Each prompt declares its focus, and the backend stitches that project’s `context.md` (plus its optional `agents.md`) together with this shared guidance.
- Treat the “Project focus” header as authoritative. Only edit files that clearly belong to that project; never assume the Agent Dev Host itself is in scope unless the prompt explicitly targets it.
- Project-specific sources can live outside `projects/` (e.g., `frontend/projects/<name>`, dedicated backend modules, or standalone docs). If you are unsure a file belongs to the current project, stop and verify before touching it.
- Per-project guidance overrides anything here. When a project provides its own `agents.md`, consider it the final word on writable surfaces and scope boundaries for that run.
- Each project now ships a `scope.yml` manifest (`allow`, `deny`, `log_only`) inside its folder. The backend reads it, surfaces the data via `/api/projects`, and appends a “Scope guardrail” block to every prompt context. Treat that block as binding instructions: stick to the listed allow globs, avoid denies entirely, and only append to paths labeled `log_only`. If the block says it is using a fallback guardrail, pause and define the manifest before expanding the writable surface.

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
- When you discover new follow-up work, queue it yourself right away (via `scripts/enqueue_prompt.py` or `scripts/plan_prompt_queue.py`) so the system keeps feeding us tasks even without user input.
- Leave log files intact so prior attempts remain auditable. Mention skipped tests or manual verifications inline with your changes.
- When you close out a task, drop a fresh prompt onto the queue that spells out the next chunk of work (or explicitly say "no follow-up needed"). The pipeline stays busy only if we keep feeding it.
- Keep an eye on the “Queue Health” card (or `/api/health.metrics`). Status chips show how many prompts are `queued`/`running`, the “Oldest queued/running” tiles list the prompt IDs that have been waiting the longest, and the badges warn when wait times exceed 60 s (`Slow queue`) or runs last longer than 10 minutes (`Long runs`). If those badges appear or the oldest prompt IDs stop changing, treat the queue as stuck: inspect that prompt’s log, cancel/retry as needed, and only then add new work.

## Keeping guidance current
- Update this shared file whenever cross-project workflows change (logging expectations, verification standards, sandbox considerations, etc.). Host- or project-specific procedures belong in that project’s guidance file.
