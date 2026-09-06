# 0005 — Keep the living world truthful and usable

Status: Accepted  
Date: 2026-09-06  
Origin: The mechanical constraints and evidence requirements in the original handover.

## Context

A map is useful when people can recognize places and understand real activity.
Invented targets, drifting identities or a blocked canvas defeat that purpose.
Historical tool recordings sometimes lack paths or runtime working directories.

## Decision

Use actual recorded agent activity. Derive specific hierarchy from explicit
recorded paths and verified metadata; never substitute an agent's mutable current
working directory for missing historical context. Keep uncertainty visible. A
command being observed does not prove successful completion or delivery.

The hot render path is deterministic and contains no model calls. Generated
source runs in an isolated, time-boxed environment; failures fall back to the
previous usable view. Published visual decisions stay recognizable until a
justified explicit change supersedes them.

## Consequences

Dictionary recognition handles known events; Spark decides reuse versus novelty;
Astra develops compatible source changes when needed. Real-time activity can keep
updating while development runs separately. Use stable identities rather than
names alone. Preview, replay and synthetic failure tests must be clearly identified.
Open screenshots and inspect animation: HTTP 200 and green tests are insufficient.
The map is a pannable desktop workspace with a 900 × 600 minimum viewport; do not
clamp its world geometry to fit a phone.
