# Project Scope Enforcement Scheme

This outlines a concrete plan for keeping future agents locked to the project declared in the queue (e.g., `example-project`) so shared host files (such as `frontend/index.html`) stop getting edited accidentally.

## 1. Manifest-driven scopes
Every project folder now owns a `scope.yml` manifest that describes its writable surface. The format
is intentionally small so it can be edited without additional tooling:

```yaml
description: >
  Short reminder that is echoed in the prompt context.
allow:
  - path/glob/**
deny:
  - path/glob/**
log_only:
  - logs/**
```

- `allow` lists globs that future runtime guards can safely permit. Use explicit prefixes (e.g.,
  `frontend/projects/<id>/**`).
- `deny` entries override `allow` matches and call out sandboxes or shared resources that must stay
  read-only for that project.
- `log_only` is for paths that are technically writable but should only receive append-only progress
  notes (e.g., `logs/progress.log`).
- `description` is a short human summary that now shows up inside prompt contexts.

The backend’s `ProjectRegistry` reads each manifest at startup, exposes it through
`GET /api/projects`, and injects a condensed “Scope guardrail” section into every prompt context so
Codex sees the current allow/deny surface alongside `context.md` / `agents.md`. If a project has no
manifest yet, the registry falls back to a conservative default that only allows files under that
project’s folder (`projects/<id>/**`) and marks the guardrail as a fallback so operators know to
author a real manifest soon.

Starter manifests ship for the actively maintained projects so operators immediately get an
accurate guardrail summary without waiting for the runtime enforcement layer.

## 2. Runtime guard in the CLI harness
- Every queued prompt now executes through `scope_guard.py`, a lightweight wrapper that proxies the real Codex CLI (`CODEX_CLI`) and enforces the active project scope.
- The backend injects the manifest (`CODEX_SCOPE_MANIFEST`), prompt id, project id, repository root, and log paths via environment variables so the guard knows which globs to allow/deny.
- While the CLI runs, the guard tails stdout for each `apply_patch`/`bash -lc` completion, inspects the repo state, and rejects any paths that don’t match the allowed globs. Violating files are reverted immediately so the repository never drifts.
- When a violation occurs, the guard prints a summary (so it shows up in the prompt timeline), writes a structured status file consumed by the backend to fail the attempt with a clear message, and appends a JSON line to `logs/scope_violations.log` with the prompt id, project id, offending path, and timestamp.
- Operators don’t need to change how they configure Codex—`CODEX_CLI` should still point at the real binary. The backend automatically wraps it with the guard before every run.

## 3. Queue-time reminders with context specificity
- Use the parsed manifest to auto-generate a short guardrail section appended to the system prompt, e.g., “Only edit files matching `frontend/projects/<id>/**`; `frontend/index.html` is read-only this run.”
- Because the manifest is structured, these guardrails stay accurate even as scopes evolve per project.

## 4. Auditable logging
- `scope_guard.py` writes a JSON line to `logs/scope_violations.log` for every blocked path. Each line includes the prompt id, project id, offending path, timestamp, and the command that attempted the edit, giving maintainers a searchable audit trail.
- The backend reads the guard’s status file after every run so prompt attempts fail with `Scope guard blocked …` summaries. Because the guard also echoes the message to stdout, the prompt timeline shows the violation immediately.

## 5. Adding scopes for new projects
- Create `projects/<id>/scope.yml` alongside the project’s `context.md`/`agents.md`. Use the same manifest structure shown above (description + `allow`/`deny`/`log_only`).
- Point the `allow` globs at the project’s writable directories (`frontend/projects/<id>/**`, `projects/<id>/**`, etc.) and add explicit `deny` entries for shared areas that must remain read-only.
- Once the manifest exists, the backend surfaces its guardrail blurb in prompt contexts and passes it to the runtime guard automatically—no extra wiring per project is required.

## Outcome
This layered approach (manifest definition → runtime guard → prompt reminders → logging) gives both proactive and reactive defenses. Once a project’s manifest is defined, the CLI literally cannot touch `frontend/index.html`, and operators get immediate feedback plus an audit path when a scope check triggers.
