# Stage three — Follow the work

This is a proposed next iteration, not an instruction to start implementation.
It explores cross-system work journeys, explicit dependencies and waiting, and
distinguishable historical routes. The diagram uses synthetic evidence and is
labeled as a concept. Four proposed Mappy tasks and success criteria are included.

Rebuild self-contained index.html with `python3 build.py`. Publish the HTML and
sw.js to `/home/peter/Documents/Clarp/fleet-map-stage-three/` for the private URL
`https://elitebook.tailf14237.ts.net:12443/stage-three/`. Preserve the existing
map, stage-two proposal and change-walkthrough routes.

The optional feedback uses ordinary named fields plus the managed clarpForm
bridge when present. Browser mode saves versioned local drafts and exports them;
it never submits directly. The schema describes preferences, not authorization.
Native publication uses stable ID `form-fleet-stage-three-v1`, version `1`.
Do not mutate that payload or republish a changed page under the same ID.
Create a new version/ID for a changed native artifact.

Verify using `node scripts/viz_stage_three_check.mjs URL OUTPUT_DIRECTORY`.
It exercises phone/desktop layouts, the four journey states, history toggle,
delivery settling, notes restore/export, offline reload and storage failures.
Native draft/submission integration is mocked for UI tests; no real answers are
sent in verification. The native artifact's creation receipt verifies Host
publication, not physical iOS Back/submit behavior. Inspect screenshots too.
