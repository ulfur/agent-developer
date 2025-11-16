# Projects registry

This directory defines every focusable project that Nightshift can work on. Each subdirectory represents a single project and must provide:

- `project.json` – metadata (`id`, human friendly `name`, `description`, optional `launchPath`, optional `default` flag) **plus** a `spec` block that documents source repos, runtimes/toolchains, environment variables + secrets, smoke tests, and operator contacts. See `docs/project_spec_schema.md` for every field and a worked example.
- `context.md` – task-specific background that is appended to the shared `agents.md` guidance whenever prompts run under that project.
- `agents.md` *(optional)* – additional guardrails for the project. When present (or referenced via `agentsFile` in `project.json`), it is appended after `context.md` so prompts receive scoped instructions without reading the entire host guidance.

Set `agentsFile` in `project.json` if you need a differently named guidance file. Otherwise the loader automatically uses `agents.md` when it exists alongside `context.md`.

New projects can be added by creating another folder that follows this convention, filling out the schema from `docs/project_spec_schema.md`, and committing the manifest. The backend automatically scans this directory at start-up, exposes the list via `GET /api/projects`, and stores the selected project id with each queued prompt so past work keeps its original context. Invalid or incomplete specs are rejected by the loader, so watch the backend logs after editing `project.json` to confirm the project registered successfully.
