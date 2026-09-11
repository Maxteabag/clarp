# Mappy — stage two: watch the work take shape

Peter explicitly approved implementation of the stage-two HTML presentation.
Nadia delegates the work to Mappy (`mappy-f66d`), retaining Claude Fable 5.1.
Proceed now. This is implementation authorization, not another planning request.

## Start from the integrated source

Use the NEW worktree `/home/peter/GIT/clarp-worktrees/mappy-stage-two` and branch
`feat/mappy-stage-two`. It starts from Nadia's integrated feat/fleet-map, including
40db1ca's corrections and the presentation. Do not build on the old
feat/mappy-work-objects branch. Keep the old worktree intact. Set your tool working
directory explicitly to the new worktree and verify pwd before editing.

Read AGENTS.md, docs/architecture/fleet-map/README.md and accepted ADRs, especially
0011 and 0012. Read the presentation source at
`docs/presentations/fleet-map-stage-two/index.html` (or view
https://elitebook.tailf14237.ts.net:12443/stage-two/), and its README.
Use the existing durable plan Nadia supplies; don't create a duplicate.

## Four delivery tasks

1. **story — Find the story.** Choose one real evidenced workflow with edits,
   validation, a recoverable interruption and an actual published artifact.
   Produce a concise evidence storyboard: what the observer should notice and
   what is known, attributed or missing. Preserve the corrected stage-one evidence
   contracts. The storyboard is a working design aid; continue without approval.

2. **material — Give the work a material.** Develop and implement a distinctive
   visual object that keeps its identity through intent, making, checking,
   interruption, recovery and publication. Explore a few treatments, exercise
   your taste, choose the strongest and make it work. The presentation's polygon,
   seal and seam are illustrative, not mandatory forms or a ceiling on invention.
   Reduce the feeling of tiny generic cards. Show a real preview when supported.

3. **scene — Choreograph the scene.** Compose agent, work and relationships
   together. Make motion communicate activity and transfers; keep structural
   belonging visually distinct. Let completed work settle rather than loop forever.
   Improve overview readability and reveal useful detail at closer zoom without
   selection-based promotion of a workspace. Keep phone pan/pinch/tap and reduced
   motion. Judge the entire composition at normal viewing scale, not just a
   magnified isolated object. Avoid label clutter and giant empty containers.

4. **prove — Prove it without captions.** Run the real replay with action labels
   hidden. Inspect desktop and phone screenshots AND animation frames/video.
   Fix overlap, ambiguity and poor contrast. Exercise unknown evidence as well as
   successful work. Extend the authoring brief so future generated additions
   develop this direction without freezing your current artistic choices into
   universal rules. Run appropriate focused tests/build and the maintained Flow,
   mobile, real-work and sandbox checks where affected. Commit and push your
   branch, then send Nadia DONE with SHA, proof paths and integration notes.

## Design responsibility

Peter wants an intuitive, beautiful, handcrafted picture of the system. His
examples convey taste, not technical specifications. Make independent artistic
and structural decisions. No uniform-bubble requirement, obligatory directory
tree, universal bytes-to-size formula or unrelated theme reset. Keep recognizable
identities, meaningful relations, agent portraits and the optional other views.
Success is following one real piece of work and understanding its story without
opening every inspector, not adding a large number of decorative animations.

## Preserve the review fixes

Same-agent/time-window attribution is labeled, not claimed as causality. Plan
mentions are references, not handoffs. Only explicit transfer evidence may animate
a handoff. A failed && chain identifies an interruption, not its failing check;
unrelated successes cannot erase unresolved failures. Pipelines/scripts/quoted
examples must not mint exact validation results. Replay must withhold future
conclusions, refresh the inspector, hide unavailable artifact links, and return
properly to Live. Open video means the actual recorded media, not its thumbnail.
Keep host-owned bounded preview loading, streamed/ranged media, and the isolated,
time-boxed generated-source runtime. No model calls during a frame.

## Verification and integration boundaries

Use the fleet-map-verify skill and existing scripts. Run a preview on a spare
port (7702 if free), with a COPY of the live JSON library and the state database
read-only. Stop only exact child PIDs you captured. Never use broad pgrep/pkill
patterns: those previously terminated the live service. Prefer sequential gates
under host memory pressure and reuse valid test evidence instead of repeated
full suites. No schema migrations or edits to someone else's dirty codex_runner.

The live service is clarp-fleet-preview, port 7699; the global Clarp server is
separate. Do not restart either or publish to the live learning library. Inspect
live Flow source for drift from your baseline and report how to preserve it.
Nadia handles review, reconciliation and deployment after your report. This does
not require Peter to approve routine implementation choices.

## Reporting

Send a short started acknowledgment to Nadia, then continue through all four tasks
without waiting between them. Update the supplied task plan with stable IDs
story, material, scene, prove. For detached workers use clarp-background-jobs;
ordinary native Clarp turns already have their own lifecycle tracking.

Use `clarp-admin prompt --to nadia-dd02 --from mappy-f66d --text REPORT`.
On completion state DONE, include branch/full SHA, screenshot/video paths, test
results, remaining evidence limitations and concrete integration notes. Report
BLOCKED promptly if needed; preserve partial edits and explain the missing input.
