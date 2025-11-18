## Auxiliary E-Ink Display Notes

### Current layout & behavior

- **Sections**: header-left (logo/title/subtitle), header-right (UPS status), body (human + agent queues), footer-left (IP / hostname), footer-right (UTC timestamp).
- **Partial refreshes**: `display_region` is only called for the sections that changed. Queue/task events refresh the body, subtitle timer touches header-left, UPS deltas refresh header-right, and minute/IP timers cover the footers.
- **Fonts/assets**: every header/body/footer line uses bold DejaVu/Liberation faces so DU updates stay legible. Subtitles rotate every 45 s and inherit the same copy deck as the web UI.
- **Self-test**: run `scripts/eink_section_selftest.py` to paint labelled rectangles for each section. The first pass draws a full-frame overlay with numbered bounding boxes, and the follow-up per-section refreshes stash PNG previews (including `sections_overlay.png`) under `/tmp/eink_section_previews`.
- **Footer responses**: `POST /api/eink/footer_message` with `{"text": "Yes boss?", "duration_sec": 3}` temporarily overrides the footer-right clock. The right column is split into override (left) + timestamp (right) slices so both stay legible, and the manager suppresses other partials until the timer expires (or you post an empty `text`).
- **Section outlines**: set `EINK_DRAW_SECTION_BOUNDS=1` before launching the backend to draw debug boxes around each section and stash every footer override bitmap under `/tmp/eink_footer_debug/` for inspection.

### Power telemetry flow

1. **X1201 monitor** (`backend/eink/power.py`) polls I²C + GPIO (default 1 s interval, override via `UPS_POLL_INTERVAL_SEC`).
2. Snapshots are normalized in `normalize_power_payload` and pushed into `PowerTelemetryCache`.
3. `start_display_manager` registers a cache callback that (a) broadcasts to SSE/websocket subscribers and (b) calls `TaskQueueDisplayManager.handle_power_cache_update()`.
4. The display manager caches the payload and performs an inline `header_right` partial. When a full-frame render is already in progress it queues a follow-up refresh so the arrow still flips immediately after the blocking draw.

### Timers & cadence

- Subtitle rotation: 45 s (header-left partial).
- Footer clock: scheduled for the top of every minute (`_compute_next_footer_deadline`).
- Footer identity (IP/hostname): every 30 s, only triggers a refresh when the label actually changes.
- UPS refresh cooldown: 5 s to avoid thrashing on noisy telemetry.

### Environment

- Systemd service: `~/.config/systemd/user/agent-dev-host.service` (restart via `systemctl --user restart agent-dev-host.service`).
- Env overrides: `~/.config/systemd/user/agent-dev-host.env` – set SPI pins, UPS poll interval, theme defaults, etc.
- Logs: `logs/backend.stdout.log` and `logs/backend.stderr.log` (stdout is quiet by default; stderr captures HTTP access + tracebacks).

### Outstanding work / observations

1. **UPS arrow latency**: the cache callback + 1 s polling reduced lag, but AC plug/unplug events still occasionally take a few seconds to reach the panel while the web UI flips instantly. Need telemetry timestamps around `_handle_power_cache_update` vs. `_publish_power_status` to see whether I²C reads or presenter scheduling are lagging.
2. **Minute drift**: the clock uses the manager loop, so heavy queue churn can still delay the footer-right refresh. A dedicated timer/thread would fully decouple it.
3. **Body offset validation**: renderer moves the body 10 px closer to the header, but the panel still looked unchanged. Confirm actual section bounds by inspecting `section_images["body"][1]` or running the self-test.
4. **Theme polish**: ASCII arrows replaced the glitchy battery bar per operator feedback. If the UPS glyphs are still distracting, consider a simple `AC/BATT` text suffix or icon drawn directly in the renderer.

Document updated: 18 Nov 2025.
