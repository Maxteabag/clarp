# 0003 — Preserve agent cabinets as a separate view

Status: Accepted

Date: 2026-09-06

## Context

The owner wanted the older World restored, but liked seeing each agent's activity
side by side in the Jacquard design. Discarding that experiment would lose a useful
view; making it replace the main world would repeat the disruption.

## Decision

Keep World as the default and Agent cabinets as a separate user-selected option.
The cabinet view retains side-by-side agent activity and nested checkout cabinets.
Choosing a view is a local display preference, not a request for AI regeneration.

## Consequences

Persist the selected view and preserve independent cameras and fallback sources.
Ordinary World evolution does not silently rewrite the cabinet option. Both views
use the same recorded fleet activity. Keep historical designs when restoring or
superseding one; do not destroy the only copy of a useful experiment.
