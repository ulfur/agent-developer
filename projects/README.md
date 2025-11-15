# Projects registry

This directory defines every focusable project that Nightshift can work on. Each subdirectory represents a single project and must provide:

- `project.json` – metadata (`id`, human friendly `name`, `description`, optional `launchPath`, and optional `default` flag).
- `context.md` – task-specific background that is appended to the shared `agents.md` guidance whenever prompts run under that project.
- `agents.md` *(optional)* – additional guardrails for the project. When present (or referenced via `agentsFile` in `project.json`), it is appended after `context.md` so prompts receive scoped instructions without reading the entire host guidance.

Set `agentsFile` in `project.json` if you need a differently named guidance file. Otherwise the loader automatically uses `agents.md` when it exists alongside `context.md`.

New projects can be added by creating another folder that follows this convention. The backend automatically scans this directory at start-up, exposes the list via `GET /api/projects`, and stores the selected project id with each queued prompt so past work keeps its original context.
