# 0010 — Mobile map navigation

Status: Accepted. Explicit owner request, 2026-09-06.

## Context

Peter now asks for a mobile-friendly map after receiving tailnet links. This
supersedes the desktop-only 900 × 600 gate in ADR 0005 and the earlier vision.
The other runtime, evidence and stability requirements remain in force.

## Decision

All map views render on phones as well as desktops. Support one-finger pan,
two-finger pinch zoom anchored under the gesture, and tap inspection. A drag,
pinch or cancelled gesture must not accidentally inspect an object.

Adapt controls to narrow portrait and short landscape viewports, with reachable
touch targets, safe-area spacing and a scrollable details panel. Keep the canvas
available between controls. Fit uses that available space; it does not reshape
the world or promote one workspace. Pinch and pan explore the same overall map.

## Consequences

The full overview has small details on a phone; users zoom to inspect them.
Responsive chrome must not redefine entity size or introduce focus expansion.
Keep desktop mouse/wheel interaction and isolated rendering. Verify phone-sized
screenshots plus real browser touch gestures, all views, and desktop regressions.
