# 0001 — Develop visual software, not only icons

Status: Accepted  
Date: 2026-09-05  
Scope: Creative capability; constrained by the continuity decision in 0002.

## Context

The first implementation classified tool calls and offered a few shapes and
small drawing recipes. It could not realize the owner's main idea: an AI-authored
map that develops its own visual systems as it encounters meaningful new things.

## Decision

The creative tier can write executable visual source modules, use ordinary
algorithms and the full Canvas API, extend the scene model and hierarchy, and
create local mechanisms and animations. Repositories may contain workspaces and
files; platforms may contain organizations, repositories and activity. These are
examples of richer representation, not an exhaustive vocabulary.

## Consequences

The creative model receives the current source and observed evidence. The
published program runs independently of model calls. Mechanical sandboxing
protects the application without reducing creativity to a fixed icon/shape list.
Capability to rewrite source is distinct from justification to disrupt an
established representation: 0002 governs when and how changes are made.
