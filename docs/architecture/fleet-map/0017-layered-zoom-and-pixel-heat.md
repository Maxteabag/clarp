# 0017 — Layered zoom and pixelated heat

Status: Accepted. Owner request, 2026-09-11.

Provide Smooth / Pixelated as display choices over the same Gaussian density.
Pixel cells sample the field on a world-anchored, nested power-of-two grid.
Panning never relocates those cells relative to entities. Zoom changes the grid
resolution to keep the blocks readable; this is coarser presentation, not a new
activity score. Persist the style independently of whether heat is enabled.

For Flow, zoom reveals observed component areas at the existing middle detail
level and their files at the close level. Membership comes from recorded paths
relative to the actual repository. Root files occupy shared space inside that
repository, outside component boundaries. No filesystem scanning or inferred
historical cwd is used. Repository and component positions do not depend on zoom.
Only the overview visibility changes. Each component shows up to four recorded
files with an explicit total; root files are similarly bounded and disclosed.

The explicitly requested Clarp project contains repositories whose recorded
origin is Maxteabag/clarp or Maxteabag/clarp-ios. Retain separate repository
boundaries, with desktop as a component of the main repo and iOS as a neighboring
repo. Other repositories remain grouped by existing verified Git metadata.
Do not infer family membership from similar names. Hidden file positions remain
available to the heat layer so zooming does not move their activity to repo centers.
