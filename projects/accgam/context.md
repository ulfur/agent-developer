accgam is a standalone 3D tic-tac-toe experiment that lives in `frontend/projects/accgam`. When focusing on this project, prioritize gameplay polish, rendering performance, and UX improvements that make it easy to explore the rotating board on both desktop and touch screens.

## Scope guardrails
- Treat `frontend/projects/accgam/` (HTML, JS, CSS, and assets) as the only writable surface. Do not touch the Agent Dev Host UI (`frontend/index.html`, shared Vue/Vuetify components, or global styles) unless a prompt explicitly asks for cross-cutting work.
- Any request about visuals, themes, or color schemes must be satisfied by editing `frontend/projects/accgam/styles.css` (or other files in this folder) rather than the host.
- If a change would normally live elsewhere, pause and explain why before editing anything outside this directory tree.

Bug fixes and enhancements should stay sandboxed inside the accgam project so they do not disturb the main Agent Dev Host interface.
