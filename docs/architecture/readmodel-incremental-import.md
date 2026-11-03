# Design: an authoritative incremental import and an import-free `/log`

Status: **revision 5, for a fifth findings-only review** (refactor slice S5).
Author: rf-readmodel for the lead (hobbles-18b0). Base: `origin/main` 9297ea75.
Revision 1 (a4dbc9d4) was reviewed by rf-review-rest-a79f
(`reviews/s5-import-design-a4dbc9d4509e.md`, 7 findings, F1-F7 below); the
lead decided Q1-Q6 on 3 Nov. Revision 2 (4551b8a0) was reviewed again
(`reviews/s5-import-design-4551b8a090ad.md`, 4 findings, G1-G4), and revision 3
(bff6295e) once more (`reviews/s5-import-design-bff6295ec909.md`, 1 finding, H1,
on G2's fence), and revision 4 (c31de92e) once more
(`reviews/s5-import-design-c31de92e1880.md`, 1 finding, I1: the fence's
`messages` scope). Findings cited
by id are in the read-model review at 09ee9279. Section 9 maps every review
finding to where it is handled.

Nothing here is built. Phase 1 moves no code; this is the plan for Phase 3
("incremental transcript import") and the RM-02/RM-13 part of Phase 2.

## 1. The problem, measured

- **RM-01.** Every watcher burst (Claude) and every `/log` that sees a new file
  signature (all backends, at most once per 5 s per agent) runs a full parse
  and a full `_store_transcript_turns_txn` walk. Live telemetry (24 h to
  2026-11-03 04:47 UTC, `/log` path only): 1,594 imports, mean 5.6 s, max
  28.8 s, 8,924 s of wall time; one Codex agent 474 × 15.7 s = 7,443 s/day on
  a 796 MB rollout.
- **RM-08.** The incremental import exists only as a disabled shadow.
- **RM-02.** A cold `/log` imports synchronously on the HTTP thread (about 30 s
  for the 796 MB session).
- **RM-13.** Only Claude has a watcher; every other backend imports only when a
  client polls `/log`.
- **RM-N1** (found by the S5 corpus, registered, fix lane). A transcript whose
  earlier record disappears shifts later turns down one slot. When an adopted
  twin row holds a slot in the shifted range, the full import raises
  `IntegrityError: UNIQUE(agent_id, backend_session_id, seq)` on every attempt.
  It is latent: no import has hit it in the last 30 days of telemetry. It must
  be fixed before any S5 parser move and before the live shadow, because every
  fallback and audit is a full import.

## 2. Frozen surfaces and the decisions already taken

| Surface | Effect | Decision |
|---|---|---|
| `/log` route, query, payload | No field change. A cold `/log` may now answer before its import finishes (stage 3). | Q2: Option B. Contract changes are out of scope (Peter), so no Option A. The first-open behaviour change is registered as an intended difference (F7). |
| SSE | No new type. `transcript-updated` is also sent after `/log`-scheduled imports that wrote rows. | Q3: intended difference, reviewer B checks it, the lead signs it. |
| `state.sqlite` | No schema change. Checkpoint carry formats change only under a new epoch (section 5). | — |
| Config | One additive key `[transcript_shadow] mode`, with an unchanged default (section 6). | Q1: additive and not frozen. Registered when it ships. |
| Env vars | `CLARP_TRANSCRIPT_SHADOW=0` stays the kill switch and forces `off`. | — |
| Message ids | Unchanged. | RM-17 is separate. |
| Live config writes | Only the release owner, in a window. | Q6: 7 days, `sample_every = 5`, after go/no-go 1 and after RM-N1 lands, with an automatic switch-off above 60 CPU-s/h (section 7). |

## 3. What the S5 tests established (3 Nov, synthetic corpus, all green)

`tests/readmodel/` at 31a32bb5:

- **Store and parse goldens** for six backends across growth steps (append, a
  partial last line, truncation in place, rotation to a new inode). They pin
  the exact stored JSON bytes (see N3).
- **The incremental path**, for Claude, Codex and agy: resuming from
  checkpoints at every growth step and at every cut gives the full import's
  rows, revisions and clock. Running the shadow changes no message row. The
  decisions are pinned: `no_checkpoint`, `incremental`, `shrink` on
  truncation, `inode` on rotation.
- **The checkpoint guard** (section 5). It caught three planted changes: a
  key-order change in a parser, an off-by-one in the carry writer and an
  off-by-one in the carry reader. The parse and store goldens alone missed the
  two carry changes.

Two new findings from that work:

- **N2: the shadow reports false divergences.** A tail turn within reach of a
  prefix turn is attributed to it by time (`TurnIndex.trace_at`: up to 120 s
  after a settled turn). `stamp_turn` then moves that turn's summary off a
  prefix row. The real tail store does the same, so its rows equal the full
  import's. The plan applies no writes, so when it stamps, the turn's new rows
  are not there and it sees no change. `_Shadow._compare` then reports
  `diverge:changed_rows`. (Revision 2 blamed the compare for ignoring
  `plan["updated"]`. That was wrong: the plan never issues the UPDATE.) This is
  registered as **RM-N3** and owned by S5. The fix, 43da9bf9 on
  `fix/rm-n3-shadow-compare`, has reviewer B's SOURCE YES and lands after
  go/no-go 1. A changed row the plan did not return counts as planned only
  when its content is unchanged, it lost its turn summary, and every current
  holder of that summary is a planned row. (A first version that exempted the
  whole turn, 86c8b628, could hide a real extra write and was replaced.) It
  must land before the shadow runs. Its two residuals are in section 4
  (item 2) and section 7 (point 1).
- **N3: key order is part of the contract.** The import decides "unchanged"
  by comparing `tools_json`/`display_cells_json` as strings. A pure move that
  only changes the order in which a parser builds a dict would give every
  row of every conversation a new revision on its first import after deploy:
  each client delta then carries the whole history. Rule for S5 moves:
  dict construction order is preserved, and the byte goldens enforce it.

Observed, not a finding: by the same time rule, a heartbeat turn that follows a
turn within two minutes is attributed to that turn's trace, so the turn
summary moves onto the heartbeat reply. This is pinned in the store goldens.
Whether that is intended is a question for the read-model owner later, not this
design.

## 4. Missing pieces before the incremental import can serve

1. Live evidence (stage 1), after RM-N3 (N2) and RM-N1 land. RM-N3 has
   reviewer B's SOURCE YES on 43da9bf9 (`reviews/rmn3-r2-43da9bf9b718.md`) and
   lands after go/no-go 1.
2. **Destination-summary validation (RM-N3 residual R1, inherited).** The
   shadow's comparator compares the rows the plan returns and which ids
   changed. It never checks the *value* of a turn's summary (`turn_json`) on
   the rows after the import. Reviewer B showed this with two separate pieces
   of evidence (`reviews/rmn3-r2-43da9bf9b718.md`):
   - On the fixed candidate, an end-to-end import that moves a summary and
     then corrupts the new holder's `turn_json` gives `match`. On base the same
     case is rejected, but only by the old false positive (`changed_rows` on
     the moved summary), not by any check of the value.
   - The blind spot itself predates the fix: a comparator control with no
     summary movement, holder corrupted, gives `match` on exact base too.

   Before a shadow `match` counts as complete parity, and before `serve`, this
   is resolved or explicitly accepted by the lead. Proposed resolution: for
   every trace the import stamped, recompute the expected summary read-only,
   from the stored rows and the `turns` row, with the same function
   `stamp_turn` uses. Then compare it with what is stored, covering every case
   `stamp_turn` has:
   - **Settled turn** (`settled_at` set): exactly one holder, the turn's last
     row in `(timestamp, seq)` order, and its `turn_json` equals the
     recomputed summary (status, times, `tool_count`). Every other row of the
     trace has `turn_json` NULL.
   - **Active or unsettled turn** (`settled_at` NULL), or a trace with **no
     `turns` row**: `stamp_turn` writes no summary, so the check requires
     **zero holders**. Every row of the trace has `turn_json` NULL. A stale
     summary left on any row is a divergence.
   - **Trace with no rows** (`stamp_turn` returns early): nothing to check.

   The rows are read across backend sessions by `(agent_id, trace_id)`, as
   `stamp_turn` reads them (I1). Both paths stamp through the one function, so
   this consistency check covers the column that the plan cannot predict.
   Controls, each of which must diverge (`summary`):
   - a corrupted holder on a settled turn;
   - a second holder;
   - a holder on the wrong row;
   - a summary left on an active turn;
   - a summary on a trace with no `turns` row.
3. A probe pass for the tail store. `store_transcript_tail` opens a locked
   writer at once, so an unchanged tail would take the writer lock every tick.
   It must use `store_transcript_turns`' probe-then-lock loop.
4. Checkpoint advance from a tail parse (section 5.2).
5. Explicit epochs instead of the source hash (section 5.1).
6. The `transcript/import` diagnostic on the watcher path.
7. Equivalence for twin cleanup and cross-area effects outside the tail
   (section 5.3), or registered differences.

## 5. Checkpoints: epochs, advance and audit

### 5.1 Explicit compatibility epochs (Q5, F3)

`parser_version(backend)` today hashes nine source files, so every release that
touches them refuses every checkpoint. It is replaced by
`PARSER_EPOCH = {"claude": n, "codex": n, "agy": n}`. A checkpoint is valid
only when its epoch matches. The source hash is still computed and logged with
every `import_shadow`/`import` event as a diagnostic, never as a gate.

A checkpoint written by old code must give the right rows when new code reads
it. One epoch covers all three things a checkpoint depends on: the parse, the
boundary and parser carry, and the store carry, both how it is written and how
it is read. The guard is in place now as tests
(`tests/readmodel/test_readmodel_checkpoint_guard.py`):

- **Checkpoint goldens**: the stored row after each growth step, carries as
  exact JSON. A change to the carry writer fails here.
- **Old checkpoint, new reader**: each golden checkpoint is restored beside
  its step's rows, and the next step resumes from it on the current code; the
  result must equal the next store golden. A change to how the carry is read
  fails here.
- **Epoch manifest** (`goldens/checkpoint-epochs.json`): the fingerprints of
  the parse, store and checkpoint goldens per epoch. When they change without
  a new epoch the test fails, and regeneration appends one. Once
  `PARSER_EPOCH` exists, it must equal the manifest's newest epoch.

What this does not do: a finite corpus cannot represent every record shape
(F3). Three other controls remain:

1. A review rule: every change to the files `_VERSION_SOURCES` or
   `_COMMON_SOURCES` lists today (parsers, and the store, carry and markup
   code) states in its review packet whether it needs an epoch bump, and the
   reviewer checks that.
2. The periodic full audit (5.4).
3. The corpus grows whenever a live shadow divergence reveals a shape it lacked.

### 5.2 Advancing a checkpoint from a tail parse (F2)

When serving, a tail import writes the next checkpoint in its own final
transaction:

- **Absolute positions**: `boundary_offset = old.boundary_offset + local
  offset`, `boundary_seq = old.boundary_seq + local index`,
  `turn_count = old.boundary_seq + len(tail)`.
- **Prefix digests (G4).** `prefix_turns_hash` becomes one per-turn fold.
  Full and tail imports compute it the same way, so the result does not depend
  on how the appends were split:
  - `d_i = blake2b-128(json.dumps(turn_i, default=str))`. The bytes keep the
    parser's key order (N3); today's digest uses `sort_keys=True` and is blind to it.
  - `h_0 = blake2b-128(b"clarp-prefix-turns/1")`, and
    `h_(i+1) = blake2b-128(h_i || u64be(len(d_i)) || d_i)`.
  - The checkpoint stores `h_seq` for its boundary. A tail import continues the
    fold from the stored value over the tail turns before the new boundary.
    A full import folds from `h_0`.

  That is a format change, so it takes an epoch bump. A test computes the same
  final prefix three ways (one full import, one large append, several small
  appends) and requires one value. The prefix byte hash is recomputed from
  the file as today; it is sampled, so this is cheap.
- **Parser carry**: computed from the old parser carry and the tail turns before
  the new boundary (unfinished Claude tool ids, sub-agent ids, agy step and
  background state). Each backend gets a function `advance_carry(old, tail_prefix)`
  with an equivalence test: the advanced carry equals the carry computed from
  the full parse, at every cut of the corpus.
- **Audit bookkeeping**: a tail import sets
  `imports_since_audit = old + 1` and keeps `audited_at` (the time of the last
  completed full verification). Only a completed audit (5.4) resets both. A
  test with an advancing clock and continuous appends proves the audit runs
  after `audit_every` imports or `audit_seconds`, whichever comes first.

### 5.3 Equivalence outside the tail (F4)

The full import does work that reaches before the boundary. The tail import
must either do the same or fall back to a full import. Rows a client sees are
never allowed to differ:

- **Twin cleanup of prefix turns.** The cleanup deletes a leftover `live:` row
  when a related reply of its own turn landed in this import, and it deletes
  final twins next to an already imported reply. A late completion of a prefix
  turn (a `live:`/`final:` row whose trace is not resolved inside the tail)
  is therefore work the tail cannot see. **Rule:** `decide` returns
  `twin_outside_tail` (a full import) when any `live:` or not-yet-adopted
  `final:` row exists for the conversation whose trace is not the trace of a
  tail request. This costs one indexed query (`idx_messages_final_rows` covers
  `final:`; `live:` rows are few). The shadow measures how often it fires.
- **Turn summaries of prefix turns** (`stamp_turn`) already behave the same on
  both paths (N2; the tail store re-stamps a prefix trace a tail row is
  attributed to).
- **Cross-area effects** (team capture, Oracle completion, Dreaming, heartbeat
  and leader-noop accounting). The full import re-runs them for every
  assistant row. That re-run is what recovers an effect that failed after its
  rows were committed. **Rule:** the checkpoint carries
  `effects_complete_seq`: the highest seq up to which one completed import
  ran every effect. A tail import runs effects from that seq, not from the
  boundary. It is set only when an import that ran the effects commits fully,
  and only up to the first row with an effect that did not report success.
  Every effect helper therefore returns an explicit outcome (`done`,
  `not_applicable`, `failed`). Calling a helper, or committing its rows, is
  not proof that its effect landed.
  The remaining difference is fewer repeated calls to idempotent helpers, and
  far fewer `teamLeaderNoop` log lines. This is registered as an intended
  difference, with tests for a failed effect retried by the next tail import
  and for an effect whose rows landed in an earlier batch.

Tests before serving: a late prior-turn `final:` row, a late prior-turn
`live:` row, a failed and then retried effect, replace-revision and head
movement on deletions, and a client reconnecting with an old cursor.

### 5.4 The audit compares one source frontier, fenced (F1, G2)

A due audit must compare the two paths on the **same bytes and the same
database state**, not the stored state before and after. Ordinary growth or a
concurrent writer would otherwise look like a repair. Reading "one snapshot"
is not enough. `plan_transcript_tail` ends its read transaction, and the full
import then runs its own probe and locked passes. Its locked pass also commits
in batches, so other writers can run between them. The comparison is
therefore fenced by a **conversation generation**.

**The generation G** of a conversation is the tuple of what the import reads
besides the transcript:
- **The agent's message head (I1):** `MAX(revision)` and `MAX(replace_revision)`
  over every `conversation_heads` row of the agent, not just `(agent, bsid)`.
  The import's `messages` reads are scoped to its conversation, but
  `stamp_turn` reads a turn's rows by `(agent_id, trace_id)` across every
  backend session (`message_writes.py:1538-1541` at base). A row of the same
  trace in another conversation, such as one carried over by
  `carry_conversation`, therefore changes which row holds the summary and
  the turn's `tool_count`. Every writer of `messages` rows bumps its own
  conversation's head with a value from the one monotonic `message_clock`:
  client rows, markers, live upserts, settles, finals, deletes, the import
  and `stamp_turn`, which bumps the head of each row it rewrites. So any
  foreign write to any of the agent's rows raises one of these maxima. It is a
  single read on the `conversation_heads` primary key prefix `(agent_id)`. A
  guard test enumerates every statement in `server/lib` that writes `messages`
  and asserts that each one moves its conversation's head.
  **One known exception at base (reviewer B, 2cbbab9c review):**
  `relink_client_message` (`message_writes.py:63`) sets a user row's
  `trace_id` with no revision and no head bump. Both callers in
  `turn_dispatch.py` (about lines 1610 and 1635) call it inside the dispatch
  transaction, but nothing there bumps either. The import reads that
  `trace_id` (`current_request_trace`), so a relink between plan and import
  escapes the fence. `/log` returns `trace_id` on every row, so delta clients
  also keep the old value. This is registered as a pre-existing bug for the
  fix lane: the relink must bump the row's revision and the head in the same
  transaction. Until it lands, the guard test fails on this writer by design,
  and no shadow or audit comparison counts. The import's own
  lookups of `prompt_admissions` are covered through the client rows they
  produce. This rule is conservative on purpose: a write in an unrelated
  conversation of the same agent also marks the comparison `concurrent`. That
  needs no proof that a trace stays in one backend session. Agents rarely
  write two conversations at once, and the shadow counts how often it happens.
- **An exact fingerprint of the agent's `turns` rows (H1).** This is the
  blake2b-128 of every row's `(turn_id, trace_id, started_at, settled_at,
  outcome)` in `turn_id` order: the columns `TurnIndex` and `stamp_turn` read.
  Outcomes change tool statuses and summaries without touching the head.
  Counts and maxima are not enough: an older turn can settle, or change
  outcome, without moving any maximum.
- **An exact fingerprint of the conversation's `agy_turn_authority` rows.**
  This is the blake2b-128 of every row's `(trace_id, assistant_start_ordinal,
  assistant_end_ordinal, terminal_status, authoritative_message_id,
  updated_at)` in `trace_id` order. Two updates can share a timestamp, or stay
  below another row's.

The head can stay a pair of maxima. Every writer of `messages` takes its
value from the one monotonic `message_clock`, so any foreign row change raises
it (the guard test above enforces this). The two fingerprints cost one indexed
scan each per batch: `idx_turns_agent_ts` and
`idx_agy_turn_authority_import`. A busy agent's `turns` may hold thousands of
rows, and `TurnIndex` reads them all already, once per import. If measured
batches show this to be too slow, the alternative is a monotonic revision
column on `turns` and `agy_turn_authority`, bumped from `message_clock` by
every writer. That is a schema expansion (expand, then contract, through
the contract owner), so it is not the first choice.

**The protocol** (the same for the stage-1 shadow and the stage-2 audit):

1. Read the file identity, then parse the whole file once. If the identity
   changed during the parse, skip (`skipped:file_moved`).
2. In one deferred read transaction, read `G_plan` and plan the tail from the
   checkpoint (`plan_transcript_tail`).
3. Run the authoritative full import. At its first `BEGIN IMMEDIATE`, and again
   at every later batch's `BEGIN IMMEDIATE`, it reads G. The first read must
   equal `G_plan`. Each later read must equal G as the import itself left it at
   its last commit: its own writes are known, anyone else's are not.
4. Any mismatch marks the comparison `concurrent`. The import goes on and stays
   authoritative, but the comparison is discarded: no verdict counts, no
   bookkeeping is reset, and the next due tick tries again. This rule is
   conservative on purpose. A foreign write to any row of the conversation
   counts, including one the import later rewrites itself, such as a live
   finalizer's row the import then adopts. That is exactly the race a
   time-separated comparison cannot tell from a divergence.
5. Otherwise compare the plan with the full result: the rows, which rows
   changed revision, deletions, prefix rows rewritten, and the head. Emit
   `import_audit` with the verdict; any mismatch is a divergence. Only a
   fenced, matching comparison of an import that completed resets
   `imports_since_audit` and `audited_at`.

Tests:
- A concurrent write to a row the import later touches, such as a `final:`
  row it adopts, between steps 2 and 3, and another between two batches: both
  must give `concurrent`, never `diverge` and never `match`.
- A concurrent write to another row of the same conversation gives `concurrent`.
- A concurrent write in another conversation of the same agent gives
  `concurrent` (I1). One control writes a row with a trace the import stamps,
  in another backend session, which changes the summary holder or the
  `tool_count`. Another writes an unrelated row there; it is `concurrent` too,
  by the conservative rule.
- A write in another agent's conversation, or a write by the import itself,
  gives neither.
- A `turns` outcome settling, and an `agy_turn_authority` row changing, between
  the plan and the import give `concurrent`. The controls include an older,
  non-maximal turn whose outcome changes with every timestamp maximum
  unchanged, and an authority update that keeps the same `updated_at`, or
  stays below another row's (H1).
- Planted prefix, carry, deletion and head mismatches on a quiet conversation
  each give `diverge`. The replay harness's planted-change list gets these
  four entries.

The fence lives inside the import's own write transaction, so it costs a few
indexed reads per batch.

## 6. Modes and their precedence (Q1, F6)

| `mode` | `enabled` | `CLARP_TRANSCRIPT_SHADOW` | Result |
|---|---|---|---|
| absent | absent or `false` | any | `off` (today's default) |
| absent | `true` | not `0` | `shadow` (today's behaviour) |
| `off` / `shadow` / `serve` | any (ignored) | not `0` | as written |
| invalid value | any | any | `off`, plus a `transcriptShadowSettingsFail` log line |
| any | any | `0` (or false/off/no) | `off` |

- A release **never** changes the default: installing it on a Host with no
  `[transcript_shadow]` section, or with `enabled = false`, leaves the shadow off.
- `serve` is only ever set explicitly, by the release owner, in a window.
- **Mode changes take effect cleanly.** The import cache skips a file whose
  signature it has imported, so changing the mode alone would not re-import
  unchanged bytes. The mode becomes part of the cache key (a mode generation
  folded into `_imported`'s signature), and any change from `serve` to
  anything else deletes the checkpoints and schedules one full import per
  checkpointed transcript, through the existing worker and window. The next
  ticks after a rollback are then full imports of current bytes.

## 7. Stage 1 — the live shadow (Q6, F5)

**What the shadow writes.** It writes no message rows. It does write
`transcript_import_checkpoints` rows inside the full import's final
transaction (behind a savepoint), and it emits `import_shadow` events. The
release owner's request states both.

**Cost, measured not assumed.** `ShadowBudget` checks the hour's CPU only
when it admits a run and charges the run after it completes. One run can
exceed the cap, and memory and I/O are not bounded. Before activation:

1. **Per-run worst case** (including RM-N3 residual R2: the shadow now reads
   every row of the conversation's content before and after the import to
   explain moved summaries, and that cost is not yet measured). Measure on the
   buildbox with synthetic transcripts
   at 16 MB tail (`max_tail_bytes`) and the largest transcript size seen live.
   The worst case is up to six candidate tail parses in `choose_boundary`, plus
   the plan. Record CPU, wall time and peak RSS per run.
2. **A per-run deadline.** `_Shadow` checks a budget between its steps (pre,
   boundary, plan, compare) and abandons the run (`skipped:deadline`) once it
   has passed 2 s of CPU. The check is not preemptive: a run can overshoot by
   one step, so the measured worst-case single step (point 1) is what bounds a
   run. `max_tail_bytes` drops to 4 MB for the observation period.
3. **Automatic switch-off** (Q6). When the hour's charged CPU passes 60 s, the
   budget latches `off` until the config's `mode` or `enabled` value changes,
   and logs it. Skipping alone is not enough.

**Reading it.** A read-only tool (counts and codes only, never content) reports
per backend, with denominators: imports, admitted runs, skips by reason
(`not_sampled`, `load`, `io_pressure`, `concurrent`, `deadline`, `disabled`),
verdicts, checkpoint coverage and cost. A backend with little traffic does
not borrow another backend's evidence.

**Exit criteria** (lead signs, Peter at the phase gate):
- zero `diverge` over the last 3 days, with at least 500 compared runs for
  each supported backend that has live traffic (a backend under that
  threshold stays full-only);
- every `error:<step>` explained;
- `fallback` below 20 % excluding `no_checkpoint` and `parser_version`;
- the measured worst case within the deadline;
- RM-N3 residual R1 (destination-summary validation, section 4 item 2)
  resolved, or explicitly accepted by the lead.

Seven days is an observation window, not proof of cost; the controls above are
what bounds the cost.

## 8. Stage 2 — serving, and Stage 3 — `/log` without imports

### 8.1 Serving (`mode = "serve"`)

1. `import_transcript` reads the identity and the checkpoint, and runs `decide`
   (now also `twin_outside_tail`, 5.3).
2. Incremental: parse `[boundary_offset, size)`; if the identity changed during
   the parse, fall back to full. Store the tail with the probe-then-lock loop.
   Advance the checkpoint (5.2) in the same final transaction.
3. Full: today's full import, which also writes a checkpoint (an audit resets
   the bookkeeping only when it completes, 5.4).
4. Rollback: section 6.

**Expected effect (an estimate, not a bound).** The big Codex agent's 7,443
s/day falls to its full imports. Those come from audits (hourly or every 50
imports, whichever comes first), parser-epoch bumps, rewrites,
`tail_too_large`, `twin_outside_tail` and retries. At one audit an hour that
is about 720 s/day. The shadow's fallback counts give the real number before
serving.

### 8.2 One importer per Host and a bounded-wait `/log` (Q2 Option B, Q3, F7)

1. **Watchers for every file backend.** `TranscriptStreamer` binds an
   import-only watcher (no `<speak>` voicing; that stays Claude-only) for Codex,
   agy, Grok and Muse as well as Claude, and polls OpenCode's WAL signature
   every 2 s.
2. **An import ticket, fenced by source frontier and mode (G3).**
   `schedule_import` returns a `Ticket` instead of a boolean. A ticket belongs
   to one import attempt and records:
   - the **frontier it read**: the file signature captured before its parse
     (`_signature`, which includes the `-wal` file for OpenCode);
   - the **mode generation** it was admitted under;
   - a completion event and a result: `wrote` (rows or the head changed),
     `unchanged`, `refused` (queue full or a reset), `error` or `superseded`.

   Each waiter (a `/log` call, a watcher change, an `after` callback) joins
   with the **frontier it needs**: the signature it observed. When an attempt
   completes, a waiter is released only if the attempt's frontier covers its
   own, meaning the same signature, read no earlier than the waiter observed
   it. Every other waiter moves to a follow-up attempt. The cache already
   queues one follow-up while an import runs; the ticket keeps that property
   and makes it explicit. Example: import A reads v1, the file grows to v2,
   and watcher change seq2 joins. A completes and releases only the waiters
   that needed v1. Seq2's callback waits for the follow-up attempt that reads
   v2, so the latest dirty intent is never cleared by an import that did not
   see its bytes.

   **Mode fence.** A mode change bumps the mode generation (section 6). An
   attempt admitted under an older generation may finish storing rows: they
   are correct full-import or tail rows. But it must not write a checkpoint
   (the write is refused inside its final transaction when the stored
   generation differs), and its completion is reported as `superseded`. That
   releases no waiter, and they move to a fresh attempt. So a stale `serve`
   import cannot recreate a checkpoint after a rollback deleted them, and it
   cannot emit a completion for the new mode.

   Tests:
   - A paused import, then an append, then a waiter joins: the waiter's
     callback runs only after the follow-up imports the appended bytes.
   - A mode rollback while a `serve` import is in flight: no checkpoint
     afterwards, no stale completion, and one fresh full import.
3. **Callbacks are composed, not replaced.** Today one `after` callback per key
   can be overwritten by a later caller, so a `/log` could drop the streamer's
   generation-checked step. Callbacks become a list. Each one runs once, after
   an attempt that covers its frontier succeeds (point 2), and keeps its own
   fence: the streamer's binding generation and sequence, or for `/log` the
   `backend_session_id` it was asked for.
4. **A cold `/log`** (no stored history) takes a ticket and waits at most 2 s:
   - `wrote` or `unchanged` within 2 s: answer from the store, as now (most
     transcripts).
   - Still running: answer with what is stored, with no payload change. The
     callback sends `transcript-updated` once the import writes.
   - `refused` or `error`: answer with what is stored and log it. The next poll
     or watcher tick retries. Never fall back to a synchronous import.
   - A retry during the import waits on the same ticket.
   - After the wait, `/log` reads the live `backend_session_id` again, so a
     relaunch during the wait answers for the new conversation.
5. **No notification loop.** `transcript-updated` is sent only for `wrote`,
   never after a no-op import. A client that fetches a delta after the event
   therefore causes, at most, one import that finds nothing and sends nothing.
6. **Client recovery** (the registered first-open difference):
   - The import completes before the client subscribes to SSE: the next
     `/agents/snapshot` shows `head_revision` above the cached cursor and the
     documented algorithm fetches a delta. A test drives the algorithm in
     `docs/protocol.md` against a test Host.
   - The SSE stream drops during the import: same as above, on reconnect.
   - The import finds no rows: no event, and the client shows the empty
     conversation, as today for an empty transcript.
   - The PWA and iOS checks are routed by the lead. This slice cannot run iOS.

## 9. Review findings and where they are handled

| Finding (rev 1) | Handled in |
|---|---|
| F1 audit must compare one frontier | 5.4 |
| F2 audit age and count; absolute positions from a tail parse | 5.2 |
| F3 versions must cover carry semantics; corpora are finite | 5.1, plus the guard tests at 31a32bb5 |
| F4 delayed cleanup and skipped effects are observable | 5.3 (fall back or keep equivalence; effects frontier) |
| F5 the shadow writes metadata; the cap is admission-only | 7 |
| F6 mode precedence, defaults, rollback invalidation | 6 |
| F7 bounded-wait semantics, callbacks, loops, client recovery | 8.2; Q2/Q3 decided by the lead |
| Claim limit: 720 s/day | 8.1, now stated as an estimate with its sources |
| G1 (rev 2) release classification | 10 |
| G2 (rev 2) a real DB-version fence for the audit and the shadow | 5.4 |
| G3 (rev 2) tickets carry the frontier and mode generation | 8.2 points 2-3 |
| G4 (rev 2) canonical, partition-independent prefix hash | 5.2 |
| Scope notes (rev 2): `_COMMON_SOURCES` in the review rule; effect outcomes; a deadline that is not preemptive | 5.1, 5.3, 7 |
| H1 (rev 3) maxima and counts miss non-maximal and same-timestamp changes | 5.4 (exact fingerprints, controls) |
| I1 (rev 4) `stamp_turn` reads a trace across backend sessions | 5.4 (agent-wide head maxima, cross-session controls) |

## 10. Releases (G1)

Two separate questions. How a release takes effect: the importer runs in the
HTTP server process, and `install.sh` restarts only `clarp.service`, so no
stage forces a runtime restart. How a release is **classified**: the lead's
rule says a release is runtime-class when its predicted `RUNTIME_RELEASE_ID`
differs from the live one. That ID hashes `server/lib`, so every code stage
below is runtime-class: the importer, the cache, the conversation service and
the watcher all live in `server/lib`. Those releases go at **18:00 only**, at
most one a day, with the release owner's prediction, its runtime follow (the
runtime rolls naturally at an idle boundary), and the canary prerequisite where
it applies. Config-only actions (switching the shadow on, `serve`, a rollback)
are not code releases. The release owner checks their identity effect when
planning them rather than assuming a label. Per the runbook, the ID hashes
the config's `[env]` table but not `[transcript_shadow]`, so these actions
should leave it unchanged. Using `CLARP_TRANSCRIPT_SHADOW` through `[env]`
instead would change it.

The order:

1. RM-N1 (fix lane; runtime-class).
2. RM-N3, the shadow comparison fix, prepared as `fix/rm-n3-shadow-compare`,
   plus the shadow's controls (section 7) and the fence (5.4): runtime-class.
3. Stage 1 (config), by the release owner after go/no-go 1.
4. Stage 2 code, shipped with the mode default unchanged (runtime-class).
   `serve` is a later config action, after the replay passes.
5. Stage 3 on its own runtime-class release, after Stage 2 has served for
   3 days with no audit divergence.

## 11. Open points for the lead

- None blocking this design. Settled since revision 2: N2 became RM-N3, owned
  by S5 (fix prepared, landing after go/no-go 1). N3 is an S5 move rule. The
  heartbeat attribution is parked as Q-RM-1. Q2 is the lead's decision, and
  reviewer B's view is recorded; it is not reopened.
