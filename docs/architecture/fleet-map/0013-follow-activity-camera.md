# 0013 — Optional follow-activity camera

Status: Accepted. Explicit owner request, 2026-09-11.

The owner wants a mode that pans and zooms toward new activity, then widens
after a while to include the overall activity. This is an optional camera mode;
it does not resize entities, change layout, or make selection expand a region.
ADR 0009's overall-map semantics remain intact.

Follow starts off. When enabled, fresh recorded starts or completions direct
the camera toward their agents and known destinations. Concurrent activity is
framed together. A six-second close view alternates with at least six seconds
of overview, even during continuous activity. Old polling results and future
replay events must not trigger a new close view.

Use gentle camera easing and bounded zoom. Respect reduced-motion preferences.
Dragging, pinching, wheel zoom, inspection, Fit world or timeline scrubbing
pauses following until the user explicitly resumes it. Persist the opt-in
preference locally, with a working fallback when browser storage is unavailable.
Verify actual camera movement and controls on desktop and phone viewports.
