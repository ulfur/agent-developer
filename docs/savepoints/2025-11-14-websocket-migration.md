# Savepoint: 2025-11-14 WebSocket Migration

This snapshot captures the first cut of the event-driven queue where the backend now emits queue/prompt/health updates over a native WebSocket endpoint (`/ws`) and the frontend consumes those pushes instead of polling `/api`. It also introduces the shared `EventStreamer`/`WebSocketManager` plumbing on the server and the Vue/Vuetify realtime connection manager in the UI.

## Included files
- `backend/server.py` – manual WebSocket implementation, event broadcaster, and health ticker.
- `frontend/index.html` – realtime client wiring, queue prompt hydration via socket messages.
- `docs/savepoints/2025-11-14-websocket-migration.md` – this marker file.

## Deployment checklist
1. Restart `python backend/server.py` (or the process supervisor) so the WebSocket listener and health broadcaster threads spin up.
2. Load the frontend, sign in, and confirm the queue hydrates without manual refresh (watch the browser network panel for a single `ws://…/ws` connection).
3. Run at least one prompt: the queue card and attempt thread should update live and the console log should show "WebSocket client connected" entries.

## Rolling back
If you need to return to the pre-WebSocket polling server, tag this state after review (`git tag savepoint-websocket-migration`) and later run:

```
git checkout savepoint-websocket-migration~1 backend/server.py frontend/index.html
```

(or equivalent commit/branch) to restore the last HTTP-only build. Because the old UI relied on `/api` polling, remember to remove the `/ws` startup log noise when reverting. Keep this file around so you know which commit introduced the realtime stack.
