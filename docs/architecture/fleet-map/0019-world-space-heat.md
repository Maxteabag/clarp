# 0019 — Absolute heat field

Status: Accepted. Owner request, 2026-09-11.
Supersedes: ADR 0016's screen-space bandwidth and ADR 0017's adaptive pixel size.

Heat represents a fixed field on the map. Use Gaussian sigma 30 world units,
the existing decay and fixed color domain, and 16-world-unit pixel blocks.
Zoom only projects that field: it must not merge different neighborhoods or
change pixel-block membership. Pan and display pixel ratio likewise do not
change the field.

Sample Gaussian contributions directly into a world-anchored scalar grid before
coloring, using separable kernel products. Bound smooth display-grid resolution;
overview sampling may lose fine visual detail, but never changes kernel width.
Pixelated mode evaluates the same field at fixed world-cell centers, visiting
only cells intersecting event support so empty zoomed-out space is inexpensive.
Retain contributions from just-offscreen events. Camera projection happens last.

Verify multi-event overlap and contour samples at the same world coordinates
across zooms, plus identical pixel-block values/membership, pan and pixel ratios.
