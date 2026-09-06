# 0009 — Overall map without focus expansion

Status: Accepted. Owner instruction, 2026-09-06.

## Context

A large selected workspace beside miniaturized nearby workspaces made identical
repositories and agents appear intrinsically different in size or importance.
Peter explicitly asked to remove single-object focus and show an overall map.
This resolves the sizing exploration in ADR 0008 and supersedes focus promotion
in the initial Flow prototype, while preserving the goals of ADR 0007.

## Decision

Flow presents observed workspaces and agents together at a common visual scale.
Selection opens inspection and may highlight an item; it does not change layout,
expand a workspace or shrink other agents. Desktop pan, zoom and fit remain.
Expansion and focused interiors are deferred until an explicit later request.

Keep the map compact and relationships visible. Recent file detail can be bounded
per workspace for legibility, without filtering out other workspaces or agents.
Use recognizable service marks and verified owner portraits, with truthful name
fallbacks, and retain useful filenames without redundant type badges (0008).

## Consequences

An overview may require zooming to read individual filenames as the fleet grows.
Do not solve that by silently selecting one workspace as the main view. Preserve
stable positions as events arrive and distinguish live work from historical
context. World and Agent cabinets remain separate choices. Ordinary procedural
novelty does not authorize restoring focus promotion or changing the whole theme.
