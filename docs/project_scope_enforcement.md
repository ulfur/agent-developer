# Project Scope Enforcement Scheme

This outlines a concrete plan for keeping future agents locked to the project declared in the queue (e.g., `accgam`) so shared host files (such as `frontend/index.html`) stop getting edited accidentally.

## 1. Manifest-driven scopes
- Introduce `projects/<id>/scope.yml` files that list explicit glob patterns for `allow`, `deny`, and `log_only` paths.
- Extend `ProjectRegistry` to parse this manifest and expose it through the prompt payload alongside `context.md` / `agents.md`.
- For `accgam`, the manifest would allow `frontend/projects/accgam/**` and `logs/**`, while denying any `frontend/*.html`, `frontend/src/**`, or `backend/**`.

## 2. Runtime guard in the CLI harness
- Wrap the Codex CLI `apply_patch` and `shell` commands with a `ScopeGuard` helper.
- The helper expands file paths touched by a command (e.g., via `git diff --name-only` before/after or by parsing the patch header) and verifies each path against the manifest globs.
- On a violation, the command is rejected before it hits git, and the agent sees an actionable error that names the offending path and the project’s allowed surface.

## 3. Queue-time reminders with context specificity
- Use the parsed manifest to auto-generate a short guardrail section appended to the system prompt, e.g., “Only edit files matching `frontend/projects/accgam/**`; `frontend/index.html` is read-only this run.”
- Because the manifest is structured, these guardrails stay accurate even as scopes evolve per project.

## 4. Auditable logging
- Whenever the guard rejects an edit, append an entry to `logs/scope_violations.log` that records the prompt id, project id, blocked path, and timestamp. This gives maintainers an audit trail.
- Optionally surface a badge in the UI when a prompt triggers a violation so operators can course-correct quickly.

## Outcome
This layered approach (manifest definition → runtime guard → prompt reminders → logging) gives both proactive and reactive defenses. Once the `accgam` manifest is defined, the CLI literally cannot touch `frontend/index.html`, and operators get immediate feedback plus an audit path when a scope check triggers.
