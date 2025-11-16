# Workspace specification schema

Nightshift now treats every `projects/<id>/project.json` file as the source of truth for how Codex should approach that workspace. In addition to the legacy metadata (`id`, `name`, etc.) each manifest **must** define a `spec` object that captures source repositories, runtime/toolchain requirements, environment configuration, smoke tests, and the humans responsible for the workspace. The backend refuses to load a workspace when the `spec` block is missing or malformed, so treat this as a contract.

## Top-level structure

```jsonc
{
  "id": "your-project-id",
  "name": "Human friendly name",
  "description": "Summary",
  "launchPath": "projects/your-project/index.html",
  "default": false,
  "spec": {
    "sources": [ { ... } ],
    "runtimes": [ { ... } ],
    "toolchains": [ { ... } ],
    "env": {
      "variables": [ { ... } ],
      "secrets": [ { ... } ]
    },
    "smokeTests": [ { ... } ],
    "contacts": [ { ... } ]
  }
}
```

### `sources`
List every repo, directory, or upstream artifact Codex depends on.

| Field | Required | Notes |
| --- | --- | --- |
| `name` | ✅ | Human readable label (“Nightshift monorepo”). |
| `url` | ✅ | Git/HTTP(S) clone URL. |
| `defaultBranch` | ✅ | Branch prompts should base off of (`dev`, `main`, etc.). |
| `vcs` | optional | Defaults to `git`. Use other identifiers if needed. |
| `path` | optional | Absolute workspace path (e.g. `/workspaces/nightshift`). |
| `description` | optional | Freeform details about what lives there. |

### `runtimes`
Capture the interpreters or languages that **must** exist for day-to-day work (Python, Node.js, Go, etc.). Each entry requires a `name` and `version`, plus an optional description that clarifies what uses that runtime.

### `toolchains`
Document supporting build/install tooling (Docker, pnpm, Poetry, Unreal, etc.). This list may be empty when a project is purely static, but populated entries behave like `runtimes`: `name`, `version`, optional `description`.

### `env`
Describe every environment variable the workspace expects along with sensitive secrets.

- `variables` entries define `name`, `description`, optional `default`/`example`, and a `required` boolean.
- `secrets` entries define `name`, `description`, optional `provider`/`location`, and whether the secret is required. Use these records to flag credentials that the operators must provision outside the repo.

### `smokeTests`
List the commands operators (or CI) should run to confirm the workspace is still healthy. Each object needs `name`, `command`, optional `description`, `cadence`, and `timeoutSeconds`. The backend exposes these tests via `/api/projects` so future automation can fan them out automatically.

### `contacts`
Simple escalation list with `name`, `role`, and any relevant contact handles (`email`, `slack`, `phone`). At least one contact is required so the UI/API can surface a real owner.

## Example
See `projects/nightshift/project.json` for a complete manifest that ties together the runtime/toolchain requirements, docker smoke tests, env vars, secrets, and operator contacts for the core platform. `projects/nebulapulse/project.json` contains a lighter-weight variant for static demos.

## Adding or updating a spec
1. Create/update `projects/<id>/project.json` and fill in every section described above. Start from one of the existing files if you need a template.
2. Ensure every list contains at least one entry where required (`sources`, `runtimes`, `smokeTests`, `contacts`). Leave `toolchains` or `env.*` empty only when the workspace truly has no requirements in that category.
3. Run the backend (or `python -m py_compile backend/server.py`) to catch syntax errors, then restart `backend/server.py`. It will log and skip any workspace with an invalid spec—fix issues until the registry reports the new entry via `GET /api/projects`.
4. Commit the manifest alongside any new documentation/context files so the next prompt inherits the same contract.

Because the `/api/projects` payload now includes the normalized `spec`, downstream systems (UI, automation, human task queue) can rely on a consistent shape for future features.
