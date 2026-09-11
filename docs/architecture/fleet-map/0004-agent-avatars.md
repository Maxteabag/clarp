# 0004 — Use the agents' Clarp avatars in World

Status: Accepted

Date: 2026-09-06

Supersedes: Boat markers for agents in The Lantern Works.

## Context

Peter explicitly asked for agent avatars instead of boats. This is a focused
change to agent identity, not a request to replace the workshop/harbor world.

## Decision

World agents use their existing Clarp portraits. Prefer the exact agent's custom
portrait, then the shared app's bundled persona image where available. Use initials
when an image is missing or fails to load. Do not invent a new avatar or mascot.

## Consequences

Preserve names, movement paths, action labels and activity colors. The host fetches,
resizes and caches portraits outside the render path; the isolated renderer
receives decoded images keyed by stable agent ID. Do not enable sandbox networking
to fetch avatars. Later creative work must retain portraits as the current marker
concept. This decision does not replace the separate cabinet view's visual system.
