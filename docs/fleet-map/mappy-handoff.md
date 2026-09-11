# Mappy: first four delivery tasks

Owner: Peter. Delegated by Nadia (session `nadia-dd02`) at Peter's explicit request.
You are Mappy, the map designer and implementation owner for this iteration.
Backend: Claude; requested model Fable 5.1. Work in your own worktree/branch.

## Purpose and taste

Build an intuitive, beautiful, handcrafted living picture of work, relationships
and resulting artifacts. This is not a file explorer dressed as bubbles. Peter's
examples reveal aesthetic taste and reasoning; they are not an exhaustive spec or
limits. Make artistic decisions and implement them. Do not keep requesting plans
or routine approval. Preserve meaningful identity and continuity, not blandness.

Read AGENTS.md, docs/architecture/fleet-map/README.md, VISION.md, all accepted ADRs
(especially 0007–0011), docs/fleet-map-plan.md and server/lib/viz_creative_brief.md.
Latest owner intent wins over superseded requirements. No equal-bubble rule,
compulsory directory tree or fixed bytes-to-area formula. Preserve overall-map
navigation, agent portraits, mobile gestures, World/cabinets separation, stable
identity, honest evidence, bounded sandbox execution and reduced motion.

Complete exactly these four outcome tasks, sequentially. Create a durable Clarp
plan in your own session with these four items; keep it updated. You own actual
implementation, not merely a proposal. Investigate and choose useful scope;
return a cohesive working slice rather than an enormous unfinished framework.

## 1. Give work a visible identity

Follow one real evidenced workflow from edits through validation into its result.
Create a recognizable work object that persists across stages and can reveal the
actual resulting artifact (for example a real screenshot/preview/report thumbnail)
when provenance supports it. Inspect available native events and artifact records
first. Separate task intent, command evidence and actual outcome. Do not equate all
activity in a repository with one task, or invent progress from tool counts.

Deliver one compelling end-to-end example plus an explicit evidence contract and
truthful unknown-state fallback. Use safe host-owned preview loading; generated
source has no network or filesystem access. Keep payload and image costs bounded.

## 2. Make relationship behavior readable

Differentiate belonging, discovery, delivery and explicit collaboration through
line form, material, motion and direction. Build on the project/worktree family
relationships. A configured remote is structural; an actual push/deployment can
move a recognizable work object. A verified handoff can transfer that same object
between agents. Shared targets alone are not communication. Where a relation is
unsupported, leave it absent or explicitly uncertain.

Choose a coherent small vocabulary through design judgment. These suggestions
are not a mandatory set of pipes, threads or branches. Validate that a viewer can
tell the major relationships apart with explanatory text hidden.

## 3. Show validation, recovery and the passage of time

Make tests/builds and failures affect the relevant work object or part of a project.
A failure should leave a legible interruption; an evidenced successful rerun may
resolve it. Show observed running operations, completed results and aging traces
differently. Give projects character informed by their actual outputs, without
random skins or unrelated metaphor changes. Research may form source constellations
and culminate in a report where evidence supports it; do not simulate understanding.

Tune cadence, opacity, size and motion together. Avoid constant decorative pulses,
permanent activity loops after completion, microscopic badges, excessive labels
and giant empty containers. Preserve routes/spatial memory through quiet periods.

## 4. Prove the picture and hand back a finished implementation

Test native evidence and lifecycle boundaries; run appropriate Python/JS gates
and build. Use scripts/viz_flow_check.mjs and scripts/viz_mobile_check.mjs, extending
meaningful assertions when contracts change. Also verify the preserved views and
sandbox failures where relevant. Take screenshots AND OPEN THEM. Inspect animation
frames or video at normal scale. Include desktop, phone, live evidence and a clearly
labeled synthetic demonstration. HTTP 200 and green tests alone are insufficient.

Fix defects you see. Update authoring instructions so future procedural additions
extend this direction; do not promote your one implementation into rigid artistic
law. Commit and push only your branch. Send Nadia a completion report with commit
SHA, branch, screenshot/video paths, verified behavior, known evidence gaps and
precise integration notes. Report blockers promptly, with independent work done.

## Workspace and live-system boundaries

Start from Nadia's `feat/fleet-map` commit containing this handoff. Use your own
`feat/mappy-work-objects` branch/worktree. Never unsafe-checkout or overwrite dirty
work. Do not edit the shared codex_runner.py. No schema migration: use bounded JSON
stores under xdg data/cache paths until branch schema collisions are resolved.

The live map is a dedicated `clarp-fleet-preview` service on localhost:7699, exposed
at https://elitebook.tailf14237.ts.net:12443/viz?view=flow. Global Clarp uses :7682.
Do not deploy this feature branch over the global Clarp installation. For your
verification, run a separate foreground preview on an available local port (e.g.
7702) using a COPY of ~/.local/share/clarp-fleet-preview/library.json and the live
state database read-only. Stop your temporary processes before finishing.

Flow is now the active procedural authoring target. The JSON library retains
view_programs.world. Live Flow may have evolved since checkout: inspect its source
and preserve learned changes in your integration notes. Do not reset the live
library or rerun viz_activate_view.py against it. Nadia will reconcile your changes
and deploy after your evidence-backed report. This is a teammate handback, not a
request for Peter to approve routine implementation choices.

Useful source: static/viz-flow/{model,world,craft,effects}.js,
static/viz-world-host.js, static/lib/viz-flow-memory.js, server/lib/viz_world.py,
viz_native.py, viz_normalize.py, viz_library.py and viz_rule_author.py.

## Reporting

Use the clarp-agent-communication skill. Send from YOUR actual returned session:

    clarp-admin prompt --to nadia-dd02 --from YOUR_SESSION --text REPORT

Announce that you started and your four-task plan. Then work through all four,
without waiting for Nadia between routine steps. On completion explicitly state
DONE or BLOCKED and include deliverables. Do not send vague promises to continue.
Use the clarp-background-jobs skill for any detached workers and finish their jobs.
