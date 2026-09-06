# 0012 — Visible work objects with an evidence contract

Status: Accepted direction. Owner request relayed through Nadia's handoff, 2026-09-06.

Refines: 0007 (relate intent, observed action and evidenced outcome), 0011
(craft and logical groups), 0005 (truthful evidence, host-owned assets).

## Context

The Flow map showed agents, files and places, but not the work itself: what an
agent set out to do, what it actually did, whether it was validated, and what
came out. Peter asked to make the work visible rather than redraw containers.
Clarp already records three independent facts: task plans and items (declared
intent), tool events (observed commands, with native lifecycle where available)
and artifacts (published outcomes with media assets). Agent-to-agent prompt
admissions record explicit collaboration.

## Decision

A work object is one persistent visual identity that follows a recorded plan
from intent through evidence to outcome. Its three facts stay separate:

- **Intent** is the agent's own declaration (plan and item titles, statuses).
  Titles are never treated as measured progress.
- **Evidence** is the set of tool events by the same agent inside the plan's
  window plus a short publishing tail. Attribution is by identity and time
  overlap, is labeled as such on inspection, and never becomes a count-based
  progress bar. Tests, builds and lint runs are recognized from the recorded
  command; a chain joined by `&&` reports one exact outcome, a script using
  `;` or `||` does not and is labeled not exact.
- **Outcome** is an artifact published by the same agent session in that
  window. When the artifact has a recorded image asset, the host loads a small
  thumbnail and hands it to the sandbox as an inert bitmap; generated source
  never receives a URL or network access. Without provenance the object shows
  its type, or is explicitly closed without a recorded artifact.

Validation states are legible and persistent: a failed run leaves an
interruption on the object and on its workspace until an evidenced later
success resolves it into a fading seam. Running work looks different from
completed results and from aging traces. Finished objects dim and settle but
keep their place so routes and spatial memory survive quiet periods.

Relationships use a small vocabulary chosen by design judgment, not a mandatory
set: structural belonging is still material (necks, double rails with anchors),
discovery flows toward the agent, attribution is a quiet tether, delivery carries
a recognizable object along the rail and arrives at the remote, and explicit
collaboration is a thread with a knot between agents. A message that names a
plan ID transfers that object's seal; other messages are collaboration without
transfer. Shared targets alone are never drawn as communication. Recorded remote
check results appear at the remote repository, not inferred from a push.

Projects gain character from what they actually produced (media, writing, code
and deployments) through tint and rim ornament. No random skins. Research with
recorded sources may form a constellation that culminates in its report.

## Consequences

`viz_work.py` supplies bounded plans, artifacts and messages with the contract
text. `work.js` attributes at the playhead so replay shows stages developing.
Outcomes without a declared plan appear as outcome-only slates with an empty
intent cell rather than being hidden or given an invented plan. Payload and
image costs are bounded (plans, artifacts, messages and thumbnails are capped).

The stage-sized slate, seal glyph, stage rail and crack/seam are the first
implementation, not a new rulebook. Future authors should extend the contract
(new artifact types, new evidence sources, new relation forms) in this
direction while keeping intent, evidence and outcome distinguishable and every
unknown stated honestly. This does not repeal 0002 or authorize a redesign.
