# 0015 — Optional activity heatmap

Status: Accepted. Owner request, 2026-09-11.

Provide an optional, locally remembered Heatmap switch alongside following and
recap. It shows recorded activity density across the existing 2D map. Repeated
events warm an area; their contribution decays with a three-minute exponential
time constant and ends after fifteen minutes. The scale is fixed so quiet
regions actually cool rather than becoming the hottest region by normalization.

Use timeline time during replay; future events never contribute. Deduplicate
event records, split multi-target contributions, and place heat only at displayed
recorded targets or explicitly recorded workspace context. Never place old work
at an agent's mutable present position. Disclose unlocated activity. Bound drawing
to the hottest 128 spatial cells and disclose truncation.

This is activity, not importance, validation, success or progress. Use a soft,
low-opacity overlay, preserve the scene's layout, and align heat to the exact
camera of the completed renderer frame, including while panning and following.
