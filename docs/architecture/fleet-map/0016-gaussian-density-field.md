# 0016 — Accumulate density before coloring

Status: Accepted. Owner request following algorithm review, 2026-09-11.
Supersedes: ADR 0015's binned, individually colored glows and hottest-128 cap.

Use a low-resolution scalar grid. Deposit each event weight bilinearly, smooth
with a separable Gaussian, then map the combined density through one continuous
color scale. Composite the colored field once over the map, rather than mixing
precolored hotspots. Coincident positions may be summed exactly; do not quantize
positions into coarse world cells or discard quieter locations.

The Gaussian has sigma 30 CSS pixels and is truncated at three sigma. Screen
bandwidth is intentional: zoom changes the neighborhood under inspection while
an isolated event's peak stays approximately stable. This depicts local weighted
activity concentration at the current view scale, not events per world-area unit.
Use peak-one kernels, with the fixed color domain 0–8 decayed event weights;
saturate above eight. Never normalize by the viewport's maximum. Bilinear sampling
introduces small approximation error, which numerical checks must bound.

Keep exponential temporal decay, the 15-minute cutoff, replay filtering,
deduplication, explicit recorded locations and the completed-frame camera.
Pad the grid by the kernel support so just-offscreen events contribute correctly.
Bound grid resolution (roughly 256 cells on the longest viewport side, plus
padding), not the number of event locations. Use a 256-entry color lookup table
and a reusable offscreen canvas. Every contributing location in the support is
processed, including low-weight regions.

Verify Gaussian profile, linear superposition before coloring, smooth crossing
of the former bin boundary, pixel-ratio equivalence, pan/zoom behavior, edge
support, fixed colors and more than 128 locations. Inspect actual browser pixels
and screenshots with heat, following and recap together. Timing probes are local
measurements, not universal frame-rate guarantees.

References:
- https://www.mapbox.com/blog/introducing-heatmaps-in-mapbox-gl-js
- https://deck.gl/docs/api-reference/aggregation-layers/heatmap-layer
