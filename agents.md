# Shared Agent Guidance

These instructions apply to every prompt, regardless of which project is currently active.

## Multi-project guardrails
- The repo hosts multiple focus areas under `projects/`. Each prompt declares its focus, and the backend stitches that project’s `context.md` (plus its optional `agents.md`) together with this shared guidance.
- Treat the “Project focus” header as authoritative. Only edit files that clearly belong to that project; never assume Nightshift itself is in scope unless the prompt explicitly targets it.
- Project-specific sources can live outside `projects/` (e.g., `frontend/projects/<name>`, dedicated backend modules, or standalone docs). If you are unsure a file belongs to the current project, stop and verify before touching it.
- Per-project guidance overrides anything here. When a project provides its own `agents.md`, consider it the final word on writable surfaces and scope boundaries for that run.
- Each project now ships a `scope.yml` manifest (`allow`, `deny`, `log_only`) inside its folder. The backend reads it, surfaces the data via `/api/projects`, and appends a “Scope guardrail” block to every prompt context. Treat that block as binding instructions: stick to the listed allow globs, avoid denies entirely, and only append to paths labeled `log_only`. If the block says it is using a fallback guardrail, pause and define the manifest before expanding the writable surface.

## Getting oriented
- Start with the selected project’s own `context.md`/`agents.md` and only branch out to shared docs when they explicitly apply.
- When you are working on Nightshift (`nightshift`), also review `the_project.txt` and the repo `README.md` so platform-wide expectations stay fresh.
- Keep project-specific facts inside that project’s folder so guidance stays scoped. If you find general expectations that apply to everyone, update this shared file instead.
- Summarize each attempt in `logs/progress.log`. Include the prompt intent, key edits, skipped verifications, and remaining questions so future operators inherit context.

## Git discipline (Roadmap §0.1)
- Nightshift now enforces per-prompt branches: each run creates `nightshift/prompt-<prompt_id>-<slug>` from `dev`. Work only happens on that branch; the backend refuses to start if the tree is dirty before branch creation.
- Never touch `main` and only merge into `dev` when a prompt (or operator) explicitly asks for it. The default workflow is: commit your work on the prompt branch, keep it ready for review, then wait for the merge instruction.
- Keep the branch clean by the time you finish the attempt. If you leave uncommitted edits behind, the backend cannot clean up the branch and the next prompt will fail—commit/stash or document why the cleanup must be deferred and queue a follow-up task.
- Mention the branch name in your `logs/progress.log` summary so humans can inspect it quickly (`git status` will show it as the current HEAD). When cleanup succeeds, the backend automatically switches back to `dev` and prunes the prompt branch locally.
- Use `scripts/git_branch_smoke.py` whenever the git workflow acts suspicious (or before large migrations) to confirm the repo is clean and the automation can cut/delete branches safely. Pass `--execute` only when you intentionally want it to touch the tree.

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

### Prompt queue checklist (do this every time you add work)
1. Re-read `ROADMAP.md` and any plan docs for the active project so the new prompt references the latest priorities and explicitly instructs future agents to keep doing the same (e.g., “start by reading ROADMAP.md”).
2. Draft the task in `docs/upgrade_plan.json` (preferred) or directly in `data/prompts.json`. Include:
   - `project_id`, `priority`, and a prompt body that states the verification expectations and that follow-up tasks are allowed.
   - Status `pending` inside the plan file; set to `queued` only after the prompt is actually enqueued.
3. Queue the prompt via `scripts/plan_prompt_queue.py --plan docs/upgrade_plan.json --count <n>` (queues the next `<n>` pending tasks using the live HTTP API and refuses to finish until the prompt is persisted) **or** run `scripts/enqueue_prompt.py --project nightshift --text "$(cat prompt.txt)"`.
4. Verify the queue immediately:
   - Inspect `data/prompts.json` with `jq ".[\"<prompt_id>\"]"` so you only match an actual JSON key. `rg` can produce false positives because the file also embeds prior prompt transcripts—do not rely on it.
   - Tail `logs/progress.log` (or use `rg -n "Queued prompt <prompt_id>" logs/progress.log`) to confirm the backend logged the enqueue event.
   - Append a note to `logs/progress.log` that lists the new prompt IDs and how you confirmed them.
5. Mention the queued prompt IDs in your user response (with file+line references) so the operator can double-check quickly, and remind the next agent they may create follow-up prompts.

### Human Tasks (blockers)
- Any time a prompt is blocked on external answers, credentials, or physical work, log it in the Human Tasks queue so the operator knows what to unblock. Use the CLI helper from the repo root:
  ```bash
  ./scripts/human_tasks.py add "Need VPN to reach staging" \
    --project <project_id> --prompt <prompt_id> --blocking \
    --description "Request access from ops; cannot run tests without it."
  ```
- `scripts/human_tasks.py list --blocking-only` shows the current queue, and `... resolve <task_id>` clears the blocking flag once the dependency lands. The UI mirrors this list under the Task Queue so you can double-check what is still outstanding.
- Treat each entry as a breadcrumb trail for operators: include the prompt id, concrete ask, and why automation is paused. Update the entry (instead of spamming new ones) when the status changes or the blocker is lifted.

## Keeping guidance current
- Update this shared file whenever cross-project workflows change (logging expectations, verification standards, sandbox considerations, etc.). Host- or project-specific procedures belong in that project’s guidance file.
