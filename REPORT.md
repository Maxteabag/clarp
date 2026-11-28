# cards-host: Rust Host part of the interactive cards (DESIGN.md section 1)

## Wire shapes (what clients code against)

Gate everything on `features` in `GET /server-info`: `decision_undo`, `approval_custom_text`,
`decision_voice_match`. One `HOST_CONTRACT` bump, the number is set when it lands (currently 216
on main, so likely 217 or higher).

### decision_undo

- `POST /decisions/{id}/resolve` takes optional `"undo_ms"`: integer 0..=15000 (anything else:
  400 `{"error":"undo_ms must be an integer from 0 to 15000"}`). Absent or 0: unchanged. The
  answer is saved at once (status, answer, revision as today); its message to the agent is held
  until `now + undo_ms`. Ignored (no hold) for on-behalf answers and for the Host's own cards
  (bound-privacy reader cards), so those never show `undoable`.
- Response unchanged: `{artifact, changed, delivery_pending}`. While held,
  `artifact.decision.undoable: true` and `artifact.decision.undo_until: <epoch ms>`. Absent
  otherwise. The same fields are on the artifact in snapshots, `GET /artifacts/...` and SSE
  artifact events. When the hold ends the Host delivers the answer and emits the artifact again
  without them. `/attention` only lists pending requests, so the fields never appear there.
- `POST /decisions/{id}/undo` `{"expected_revision": n}`: n is `artifact.decision.revision`
  from the resolve answer. Owner and devices only (tools token: 403).
  - 200 `{"artifact": ..., "undone": true}`: the decision is `pending` again (answer,
    resolved_* cleared), `revision + 1`, artifact status `active`. Use the new revision for the
    next resolve.
  - 200 `{"artifact": ..., "undone": false}`: it was already pending (idempotent).
  - 409 `{"error":"too_late"}`: the agent already took it (delivered and its turn started),
    expired, cancelled, or answered on the user's behalf by an agent.
  - 409 `{"error":"decision revision changed"}`, 409 `{"error":"decision not found"}`.
  - It still succeeds after the hold ended if the answer is a queued message the agent has not
    started (the same take-back as `DELETE /turn-queue/{id}`), but clients only show Undo while
    `undoable` is set.

### Native forms and HTML forms (same feature)

- `POST /artifacts/{id}/submit` takes optional `"undo_ms"` (same range) beside `submission_id`,
  `version`, `answers`, `synthesize_audio`. The receipt gains `undoable: true` and
  `undo_until` while held.
- While held: a render_spec row (list and single read) has top-level `undoable` and
  `undo_until`, and the opened spec's `submission` object has them too. An HTML form's
  `latest_submission` receipt has them.
- `POST /artifacts/{id}/submissions/{submission_id}/undo` (body `{}` or none). Owner and
  devices only.
  - 200 `{"artifact": <row>, "undone": true}`: the submission is gone and a render spec is open
    again (status back to what it was, `ready` or `active`).
  - 200 `{"artifact": <row>, "undone": false}`: no such submission (already undone).
  - 409 `{"error":"too_late"}`.
  - After an undo, send again under a NEW `submission_id`; the undone id is refused
    (409, `submission ID was taken back`).

### approval_custom_text

- Approvals take `answer: {"text": "..."}` instead of `choice` (exactly one of the two, as
  today). Result: status `answered`, resolved_choice `answered`, answer `{"text": ...}`. The
  agent is told it is not approval.
- Approval rows say `allow_custom_text: true` on this Host. single_choice keeps its own value
  (`false` with `--no-custom`); hide the own-answer row only when it is false.

### decision_voice_match

- `POST /decisions/{id}/match` `{"text": "...", "commands": {"next": [...], "undo": [...],
  "cancel": [...]}}` (`commands` optional). Read-only. Owner and devices (tools token: 403).
  Answers exactly the four shapes of DESIGN.md 1c:
  `{"kind":"option","option_id","label"}`, `{"kind":"approval","choice"}`,
  `{"kind":"custom","text"}`, `{"kind":"command","command":"next"|"undo"|"cancel"}`.
  Empty transcript: `{"kind":"custom","text":""}`. Text-input requests always answer custom
  (commands still match first, so "neste" on a text card still moves on).
- Vectors: `contract/fixtures/decision-voice-match.json`.

No deviations from DESIGN.md except the details it left open: the undo route for forms is
`/artifacts/{id}/submissions/{sid}/undo`, an undone submission id cannot be reused, and command
words are checked before option matching on every request type.
