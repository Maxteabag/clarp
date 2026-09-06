# Stage two presentation

A self-contained HTML proposal for the next fleet-map iteration. It contains a
six-stage conceptual animation, relationship studies, a four-task brief for Mappy,
comprehension criteria, and optional locally saved/exportable notes. It sends no
messages, assigns no work and does not change the live map.

Permanent private hosting directory:
`/home/peter/Documents/Clarp/fleet-map-stage-two/`

Presentation: `https://elitebook.tailf14237.ts.net:12443/stage-two/`

Copy index.html and sw.js to the permanent directory when intentionally publishing
an update. Keep the existing root proxy on port 12443 intact. The scoped service
worker caches only this document; it never caches Clarp APIs or credentials.
Version draft keys and the service-worker cache for incompatible future revisions.

Verification from the repository:

    node scripts/viz_presentation_check.mjs URL OUTPUT_DIRECTORY

Checks desktop/phone overflow, every concept stage, playback, notes persistence,
export content, offline reload and storage failure. Open the resulting screenshots.
Concept visuals are illustrations, not observed live screenshots or commitments
to a particular implementation. The Save HTML link exports the self-contained
presentation; service-worker offline reload applies to its hosted version only.
