# Mappy — stage three: Follow the work

Peter explicitly approved this implementation: “Yes! Make him do it” after the
stage-three HTML proposal. Nadia delegates to Mappy (`mappy-f66d`, Claude Fable
5.1). Begin now; no further plan approval is needed.

## Baseline and intent

Use `/home/peter/GIT/clarp-worktrees/mappy-stage-three`, branch
`feat/mappy-stage-three`, created from the integrated feat/fleet-map. This includes
the deployed 8c38b99 lantern treatment and its review fixes, plus the proposal.
Your session's original launch cwd may still be an older worktree: use the NEW
absolute worktree as the explicit directory for tools and edits. Do not checkout,
reset or repurpose either of your previous worktrees.

Read AGENTS.md and the fleet-map vision/accepted ADRs, then
`docs/presentations/fleet-map-stage-three/index.html` and its README. The HTML is
also available at https://elitebook.tailf14237.ts.net:12443/stage-three/.
Its four tasks are approved; its illustration is inspiration, not a required
shape vocabulary or a claim that every pictured relation already exists.

The goal: follow one piece of work across meaningful agent/system boundaries;
understand evidenced waiting; distinguish present activity from historical routes.
Keep the stage-two lanterns, avatars, useful project groups, overall navigation,
mobile gestures and evidence discipline. Make your own artistic decisions. Avoid
another collection of generic cards, an unreadable mesh of lines, or an unrelated
visual theme. Whole-workflow comprehension is the test, not animation count.

## Four outcome tasks

1. **route — Find the real route.** Inspect task, native activity, message,
   validation, artifact and deployment records. Choose a real cross-boundary
   workflow with useful evidence. Write an evidence map separating explicit
   relationships, time-window attribution and unknown links. Where necessary,
   implement a small safe observation/contract extension in your branch; do not
   fabricate historical links or declare the visual feature blocked just because
   one convenient record is absent. Choose a truthful narrower pilot if needed.

2. **journey — Make the journey legible.** Implement recognizable work identity
   and purposeful motion across the chosen route. Distinguish belonging, requests,
   references, observed collaboration and actual delivery. A request does not
   prove acceptance or execution; a push does not prove service deployment.
   Develop a cohesive composition at ordinary viewing scale. You choose the
   mechanisms; the proposal's rail diagram is not an artistic constraint.

3. **waiting — Give waiting and history a form.** Represent a recorded dependency
   or interruption at its real boundary and distinguish it from quiet/inactive
   time. Let repeated observed interactions form restrained historical routes.
   Past traces must be distinguishable from moving live actions and from unresolved
   waiting. Bound visual/data accumulation. Repetition is not proof of causality.
   Preserve spatial memory and useful zoom-dependent detail.

4. **prove — Prove the full picture.** Replay actual evidence with labels hidden.
   Inspect screenshots AND motion/video in busy and quiet scenes, on phone and
   desktop. Include an honest counterexample and unknown evidence. Extend the
   authoring brief with principles/contracts, not rigid artistic rules. Run
   appropriate focused tests/build and maintained visual checks for affected
   behavior. Commit and push; send Nadia DONE with full SHA, proof paths, evidence
   limitations and precise integration notes. Do not stop at a design proposal.

Use the exact durable plan ID Nadia supplies; update route/journey/waiting/prove
sequentially without creating another plan or waiting between routine steps.

## Carry forward the integration fixes

- No model work during frames. Generated code stays bounded and isolated; image
  fetching/decoding is host-owned with size/time limits and truthful fallbacks.
- Motion requires observed running evidence; recent completed work is a trace.
  CSS-scale detail uses camera.k / pixelRatio. No invented source dots.
- Plan mentions are references. Only explicit verified transfer evidence can
  show a handoff. Same-agent/time overlap is attribution, not causal proof.
- Validation scope matters: failed chains/scripts may not identify a failing
  check. Unrelated successes or later failures cannot erase unresolved checks.
  A published artifact can retain a crack. Do not regress pipeline/quote handling.
- Replay withholds future conclusions and artifact links, refreshes the inspector,
  and returns correctly to Live. Video links open the actual media, not thumbnails.
- Preserve World/cabinets and any learned live Flow source that appeared after
  your baseline. Inspect and report drift before suggesting publication.

## Runtime and reporting boundaries

Use a COPY of ~/.local/share/clarp-fleet-preview/library.json and a read-only live
state DB for your preview, on a verified spare port (7702 if free). Use the
fleet-map-verify skill and captured child PIDs only. Never broad pgrep/pkill cleanup.
Do not restart or deploy to the live map service or the global Clarp server.
Nadia handles review, learned-source reconciliation and final deployment. No
schema migrations or writes to another agent's dirty codex_runner.py; use bounded
JSON storage where additional state is necessary. Run checks sequentially under
host pressure and preserve partial work across interruptions.

Send a started acknowledgment, continue through all four tasks, and report DONE
or a concrete BLOCKED reason using:

    clarp-admin prompt --to nadia-dd02 --from mappy-f66d --text REPORT

Report integration readiness accurately, including what is real, simulated,
attributed or unsupported. Any detached worker must use clarp-background-jobs
and finish its own durable job; native turns already have lifecycle tracking.
