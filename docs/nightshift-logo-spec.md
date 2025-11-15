# Nightshift Logo Refresh

This document covers the refreshed Nightshift marks (header tile + favicon), their construction, and how to use them in code without layout churn.

## 1. Core Concept
- Rounded 36×36 tile and 32×32 favicon.
- Monoline "N" built from a single continuous stroke with rounded caps/joins (2.6 px on the header icon, 2.2 px on the favicon).
- Forward-leaning crescent anchored to the upper-right corner, sized so its bounding circle sits 3 px from the edge.
- Two palettes: Midnight (dark UI) and Dawn (light UI).

## 2. Color + Stroke Reference
| Element | Dark Mode | Light Mode |
| --- | --- | --- |
| Tile gradient start | `#030918` @ (4,4) | `#f7fbff` @ (6,4) |
| Tile gradient end | `#142d55` @ (32,32) | `#c7ddff` @ (30,32) |
| Ambient glow | `#4ac9ff` → transparent radial (cx 30%, cy 20%, r 80%) | `#7fc3ff` → transparent radial (cx 32%, cy 24%, r 70%) |
| N stroke gradient | `#7de0ff` → `#caa9ff` | `#2563eb` → `#9333ea` |
| Crescent gradient | `#fff6d0` → `#f3c76d` | `#ffe99c` → `#f5c056` |
| Stroke width | 2.6 px (header) / 2.2 px (favicon) | same |
| Corner radius | 10 px (36²) | 10 px (36²) |

## 3. Geometry + Spacing
- Tile padding: keep art inside an inset rectangle that is 4 px shy of each side to avoid clipping at small scales.
- N glyph: baseline at y=26 (header) / y=23.2 (favicon). Apex sits 1.2 px below the top inset to avoid kissing the moon.
- Crescent: outer arc radius 5.9 px (header) / 5.1 px (favicon); inner arc radius 4.5 px (header) / 3.9 px (favicon). Use `fill-rule="evenodd"` so the smaller arc subtracts and produces the crescent void.
- Safe-area: preserve the 0.75rem gap defined in `.global-header__branding` when swapping the `<img>`. No layout offsets are required.

## 4. Simplified Fallback Mark
When gradients or masks are not available (CLI renders, terminal logs), use a single-color mark:
- Solid circle (`#142d55` for dark or `#dce6ff` for light) with radius 14 px.
- Simple `N` stroke using `stroke-width: 3px` and `stroke: currentColor`.
- Optional small filled circle for the moon at `cx=12`, `cy=8`, `r=3`.

## 5. Asset Drop-In
Files live under `frontend/`:
- `nightshift-header-dark.svg`
- `nightshift-header-light.svg`
- `nightshift-favicon-dark.svg`
- `nightshift-favicon-light.svg`

To use them outside this repo:
1. Copy the four SVGs into your static asset directory.
2. Point your header `<img>` to the appropriate theme-specific file (or bind it to theme state as we do in `frontend/index.html`).
3. Update `<link rel="icon">` tags to the theme-aware favicon (see `applyTheme()` helper for an example that rewires both the primary and alternate favicon links).

No margin/padding adjustments are required because the assets keep the 36×36 / 32×32 frames that the Vuetify header already expects.
