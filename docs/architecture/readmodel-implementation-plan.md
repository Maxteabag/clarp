# RM-01 and RM-02: implementation plan after big merge 1

Status: design only, prepared for lead 631 and reviewer C on 5 November 2026.
No production or test implementation is included. Neither finding is closed by
this plan. Nothing here enters big merge 1 or the Friday/Saturday releases.
Implementation starts after big merge 1, with new claims, intended-difference
signatures, exact gates and release authorization.

## Basis and ordering

This plan continues a4dbc9d4509e87bc8a54d165ced1f3600a34b86c and its reviewed
revision 5, fea9c9c5, in `readmodel-incremental-import.md`. Revision 5's review
fixes remain binding: summary destinations, cross-session traces, effects,
comparison fences, completion frontiers and rollback generations cannot be
discarded to make the fast path smaller.

Source inspected: main 7e3b14592db832b77b4db4e004b4def2ebc8a6b6; landed integration
merge 16 a733101781144cbfaf5e6dcdc3386cd71f445be0; staged S5 message cut
5f698140552e4b9fdd24a0a008aa01fffc18dafb and Codex cut
989779c0bced90abe07bbd9ed41079c741d999bc. These S5 packets take priority. Both
are with the captain for M17; their exact CI, G4 and handwritten forward-port
watch closure remain captain work. Do not rebase or edit those pins for this
plan. At implementation time, re-pin to the actual merged main and read the
captain's final move map first.

Also inspected, without waking or editing the paused RM-N14 owner:

- `/var/tmp/rmn14-design-note.md`, including its captain addendum and lead 582
  limits recorded in the decision log;
- `refactor-bug-rmn12/tests/readmodel/rm_n14_harness.py`, SHA-256
  `6f0e810450d0768ea5a8325b98a0077f4540d0129e254933e8ab89fdbb5fc49d`;
- RM-N12 d52416d1057cb5732a22e8bada1b92ce1e83a3a5 and C's report;
- RM-N13 088414ff17be10003eb1fa01eba996d7e9738440 and its reviewed cursor limit.

The RM-N14 harness is WIP, not a delivered fast path or a latency result. It
currently starts a fresh process for each lane/tick and explicitly reports
`candidate_fast_path_proven=False`. Keep that restart oracle and add a separate
persistent-worker lane when the owner resumes: a fresh-process lane cannot
measure retention of a process-local continuation. No harness edits now.

## Outcomes, and what remains a limit

RM-01 needs authoritative apply of eligible incremental work, not another
shadow-only path. RM-02 needs every `/log` branch to serve the canonical store
without parsing, storing or waiting on an import path lock on the HTTP thread.
An optional bounded completion wait happens outside SQL and locks; it is not a
synchronous import. Background completion must wake clients even with no open
polling client.

For this plan, a settled read is one **committed, coherent SQLite snapshot**:
conversation identity, rows, pagination, head and replace watermark agree. It
does not mean that a growing transcript is fully imported, or that a budgeted
multi-batch import publishes every row atomically. Existing batch commits stay
visible, as today. Do not claim an all-or-nothing import generation without a
separate publishing/storage design. No long transaction, whole-import writer
lock or complete-history response cache is introduced to hide intermediate
commits. Lead 635 approves this definition; atomic whole-import publication stays out of scope.

RM-N14 step 1 remains the first RM-01 delivery: Claude/Codex append-only fast
paths, existing checkpoint format, exact RM-N10 database validation and a
bounded process-local frontier. It removes full transcript parse/store work on
eligible appends. It does **not** prove strictly new-byte-proportional total work:
raw database validation remains O(database history), a restart loses the frontier,
and an unsafe or oversized frontier falls back. Those are measured limits, not
accepted closure of the original high finding for every backend.

RM-N12 is a mitigation: busy warm FULL replacements wait at most 2.25 seconds,
with a known-slow skip. Cold reads and other branches can still import directly.
RM-02 extends the background path to those branches; it does not remove the
2.25-second bound/skip or promise that large-history replacements are fresh.
Successful RM-N14 imports can restore eligibility after a previous slow sample.

## Permanent invariants

1. RM-N10: one probe snapshot; pure `_row_phases` and `_visible_replies` before
   admission; `BEGIN IMMEDIATE`; exact ordered raw database-input re-reads;
   rebuild only changed database components under the writer; no second probe
   or failure just because a prelude changed. No global `data_version`, count/MAX
   surrogate, lazy first-write BEGIN or optimistic stale row decisions.
2. Preserve RM-N7 batching, time/row budgets and yields. A checkpoint describes
   only committed work, is written in the final transaction/savepoint, and never
   advances beyond committed rows. A failed checkpoint leaves retry/full fallback
   available. Earlier committed batches must be safely replayable after failure.
3. Preserve exact message ids, ordering, JSON bytes/key order, revisions, clock,
   heads/replace watermarks, provenance, twin adoption/deletion, authority and
   turn-summary placement, including traces spanning backend sessions.
4. Helpers use the supplied writer/connection. Keep the call-time
   `message_store.conn` and `IMPORT_*` facade resolution. No unwrapping, hidden
   commit or autocommit. RM-N13 explicit transaction cursors remain native;
   autocommit prefetched RETURNING exposes only `fetchone`/`fetchall`.
5. Only the locked pass performs effects. A persisted row is not evidence that
   its team/Oracle/Dreaming/heartbeat/leader effect succeeded. Failed effects must
   be retried even when the next import touches only a later tail.
6. Parser-version protection covers every reached module, including the S5
   rows/common modules and Codex cells reached by AGY. No unchecked epoch or
   disabling old-checkpoint/epoch guards as a performance optimization.

## Work packages and ownership

Each package starts only after its predecessor's required evidence and a fresh
claim. Main fixes after big merge 1 use the merged S5 layout; any integration
branch still alive gets the captain's reviewed forward-port, never a second
independent importer implementation. Reviewer C owns read-model verdicts; a
database-format proposal additionally needs the contract owner/A2 and lead.

| Package | Concrete work | Exit and authority |
|---|---|---|
| P0: re-pin and characterize | Confirm M17/watch closure and merged base; reconcile RM-N1/N2/N3 and destination-summary residuals with the registers; enumerate actual input/effect writers and current callbacks; adopt the resumed RM-N14 harness | No new serving claim until row, summary, effect and checkpoint oracles discriminate planted errors |
| P1: tail apply, RM-N14 step 1 | Give `store_transcript_tail` the full import's probe/prepare/revalidate protocol, final checkpoint callback and safe carry handling; select eligible tail apply from `import_transcript`; preserve reference full fallback | Claude/Codex authoritative apply passes cloned-full equality after every tick, failure/restart controls and exact G1/G2/C; enabled only through the reviewed rollout |
| P2: long-turn continuation | Bounded process-local parser/store frontier at the last complete byte; affected earlier rows and pending call references; safe invalidation/eviction | Fixed-delta long-one-turn witness actually uses retained state; latency reported by component, not extrapolated |
| P3: background admission and coherent reads | Cover every cold/warm `/log` branch with background scheduling/tickets; read identity/page/head/replace in one short snapshot; import-only triggers and completion notifications for all backends | No HTTP-thread parse/store, bounded waits/queue pressure, coherent pagination/replacement and native-client recovery controls |
| P4: remaining providers and sustained proof | Backend-specific safe ranges for AGY/Grok/Muse; OpenCode WAL-aware range/row strategy or an explicitly measured remaining full fallback; shadow/audit and operational measurements | RM-01 stays open for uncovered normal change paths; RM-02 can close independently once every read is import-free |
| P5: optional strict DB incrementality | Separate writer-complete change feed/token design, if P1/P2 measurements require it | NOT authorized by lead 582/631; separate stored-contract decision and forward/back reader-first proof |

Lead 635 assigns RM-N14 step 1 (P1 and its already-designed P2 frontier pieces)
to rf-bug-rmn12 on its explicit resume after Friday's ship. S5 owns P0, P3 and P4
and coordinates the sequence. Settle exclusive function claims with the lead
before every package's edits. This plan neither wakes the paused owner nor
duplicates its harness.

Canonical S5 edit locations after the two staged cuts:

- `message_writes`: full/tail orchestration, `_ImportWriter`, RM-N10 prelude,
  `_row_phases`, `_visible_replies`, `_finish_import`, `stamp_turn`/`turn_stamp`,
  `_attach_authority_tools`;
- `message_import_rows`: `_FinalRows`, provenance/authority and row/twin helpers;
- `message_import_common`: raw provenance, normalization/revision/id helpers;
- `message_turns`: TurnIndex; `transcript_incremental`: decide/carry/checkpoints;
- parser modules plus small continuation helpers (avoid regrowing oversized
  files); `codex_tool_cells` remains the existing cell implementation;
- `transcript_import_cache`, `conversation` and `transcript_streamer` for P3.

## P1/P2: exact range apply and safe fallback

Keep the full importer as the reference and recovery path. Initially serve only
supported append-only Claude/Codex shapes. Capture source identity and complete
byte frontier; validate inode, shrink, backwards mtime, prefix/tail rewrite,
parser version, audit age and old-reference guards. Identity movement during
parse must not let a later signature be marked imported merely because parsing
finished later. Coalesce one follow-up when growth races an attempt.

Use the existing safe user/task boundary first. Long active turns require more:
retain only the mutable frontier (assistant aggregation, pending tools/subagents,
call-to-row mappings, phase-sensitive text, request/provenance cursor, authority,
twins and summary state). New tools may change an earlier assistant row's phase;
late results may change old cells. Apply those dependencies explicitly or fall
back. New user turns close dependencies only when equivalence proves it. Reuse
turn stamping only with proof that settlement/summary dependencies are unchanged.

Never publish frontier progress before its batch/checkpoint succeeds. On reset,
mode/source/owner/session/version change, eviction, unknown references or an
oversized frontier, drop process-local state and use a readable checkpoint/full
fallback. Frontiers have bounded count and byte size using existing lifecycle
and RevisionedCache patterns, not an unbounded dictionary of transcript history.

Do not introduce revision 5's new fold, carry keys, effects watermark or epoch
format silently. P1 uses the schema117 checkpoint bytes/readers as they exist.
If advancing a boundary cannot reproduce that format without walking the prefix,
keep the earlier safe boundary or recompute conservatively and report the cost.
P2's in-memory continuation is the first way around that limitation. A new
durable format is a separate reader-first package, not a hidden P1 change.

Prefix twins and effects remain correctness gates. A prefix `live:`/unadopted
`final:` dependency outside the proven frontier forces the reference full path.
Until every effect has an explicit success/not-applicable/failure result and a
safe recovery frontier, retain conservative prefix effect replay; do not infer
success from absence of an exception. Process-local successful-effect progress
may reduce duplicate work only after outcome/failure tests; restart replays
conservatively. If a durable watermark is necessary, follow the format gate
below. Register reduced idempotent calls/logs separately from row equivalence.

## P3: background import and a coherent `/log`

The HTTP thread may locate/stat a source, admit a bounded job, optionally wait on
its completion and read SQLite. It never parses, stores, acquires the path lock
or synchronously retries a failed import. Cold/adopted/restored histories use
the same rule as warm histories. Timeout, refusal, error or known-slow skip
returns the stored view and a diagnostic code; no synchronous rescue path.

Preserve RM-N12 owner/path/epoch isolation, urgent cooldown bypass, measured-slow
skip/recovery and watcher callback preservation. Retain the signed 2.25-second
maximum for applicable FULL reads; delta/older pages remain non-waiting. Propose
using that same bound for cold FULL reads rather than reintroducing the older
design's separate 2-second policy. Lead 635 approves this direction; the exact intended difference is signed on
the P3 implementation commit. A cold partial/empty response must be recognisable
as in progress through EXISTING fields. Audit the actual response fields and
shipped client state machines; do not assume an empty page or `missing` alone
provides that distinction. Prove recovery to complete history after the
completion notification on shipped iOS, desktop AND PWA clients. If any shipped
client cannot recognise or recover, return to the lead before activation,
without a payload addition or client-contract workaround.

An internal ticket identifies owner, backend session, source frontier, mode/reset
generation and terminal result. Attempt v1 cannot satisfy a waiter/callback for
v2. Identical observations join; dirty later observations get a follow-up. An
append-only covering frontier requires actual parser coverage evidence; times
alone never certify bytes. OpenCode includes the WAL signature. Preserve the
existing bounded worker capacity; do not add a pool or alter Stop/admission
ordering under the parked TP-02 work. Refusal is visible/retriable and never
clears dirty intent. Reset/drain refuses new work and invalidates old completion.

Compose callbacks instead of replacing them. Each successful covering attempt
runs each callback once after commit with its own watcher binding/sequence or
requested conversation fence. Notify with the existing `transcript-updated`
shape only for actual row/head changes, never no-op/error/refusal. A full import
being executed is not itself evidence of a change. Recover dropped events via
the existing snapshot/head cursor algorithm, without adding payload fields.

Use import-only watchers for non-Claude file backends; preserve Claude voicing
and binding fences. Poll database-backed OpenCode using its WAL-aware signature,
initially with the prior design's 2-second proposal subject to measured capacity.
Keep read-trigger scheduling as recovery for missed events/terminal-only owners.
Bound registration and tear it down with runtime/session lifetime. No worker,
timer or callback survives an ended binding as authority for a new conversation.

After any wait, reselect runtime/backend session, then build the response in one
short read transaction. Read page/lookahead, head, replace watermark and identity
from that same snapshot. Revalidate cursor/session rules without a loop that can
extend the deadline; a relaunch produces the current conversation's normal empty
or stored view. Include-tool-details and automation filters keep their current
surface. Import batching is not hidden behind a long reader snapshot.

## Compatibility, modes and rollback

P1-P3 make no schema, table, column, stored JSON-key, message-id, route, payload,
CLI, env or path change. Reuse the checkpoint reader/writer contract. Retain
source-hash invalidation, including every new dependency, for the first delivery.
Explicit epochs in revision 5 remain a later reviewed compatibility change, not
an excuse to retain stale checkpoints after parser/store semantics move.

For any later fold/carry/effects watermark or change feed: ship a compatible
reader first; old/new readers must either interpret the exact bytes correctly
or explicitly refuse and take full fallback. A version identifier does not
automatically make a novel carry safe. Never write new mandatory keys under an
identifier an old reader accepts. The contract owner must verify new->old->new
against copied synthetic databases, including direct import after rollback.
Preserve current monotonic checkpoint writes and crash-safe final savepoints.

Use revision 5's reviewed mode precedence only after its separate controls land:
unchanged default, legacy enabled=true meaning shadow, invalid mode failing off,
environment kill switch outranking config, explicit serve activation by release
owner. A rollback invalidates fast-path state and schedules a bounded full
reconciliation through the same worker. A stale attempt cannot recreate a
checkpoint or emit a completion under the new generation. The implementation
must choose a transactionally effective generation fence before enabling serve;
a bare unchecked process flag is insufficient. If that requires new durable
metadata, split and review its compatible reader rollout first.

Forward/back proof: bootstrap old checkpoint/product rows; run new full and
incremental appends; reopen with the exact previous release's reader/importer;
append/rotate/rewrite and import; reopen with new code again. Compare product
tables, exact JSON and heads after every step; metadata may differ only as
declared. Repeat after failure before checkpoint, between batches, after final
commit but before cache completion, reset, and restoration of an older database.
Losing optimization state is acceptable; lost/duplicated rows or effects are not.

## Deterministic proof and performance

All execution is future buildbox work; no test or benchmark was run for this
document. Extend the resumed owner's fixed-delta oracle rather than add a second
one. Reference/candidate databases start from identical synthetic state and
fixed clocks; compare all product tables after every tick, not only messages.
Compare external effect intents/results separately where they are not stored in
those databases. Keep checkpoint metadata a separate oracle, never exclude
product state to normalize away a difference.

Required controls:

- append, partial line, rotation/shrink/rewrite, parser invalidation, eviction,
  restarted worker, long one-turn history, late call result and old-prefix twin;
- changed same-agent TurnIndex/provenance/latest-user inputs rebuild only that
  component; unrelated SSE commits cause no rebuild; continuous churn succeeds;
- same-trace rows across sessions, older turn outcome edits and same-timestamp
  authority edits; no count/MAX or single-session fence can hide them;
- failed effects retried after later appends and after an earlier batch committed;
  corrupted/multiple/wrong/active/no-turn summary holders are detected;
- held import v1, append v2, waiter/callback v2: only v2 completion releases it;
  owner A on the same path cannot complete owner B's ticket;
- held worker/SQLite writer/queue full: cold and warm FULL return within the
  bound, known-slow skips, delta/older reads never wait or import synchronously;
- runtime relaunch/reset/rollback during wait: no old-session completion, lost
  watcher callback, stale checkpoint recreation or extra post-reset write;
- pagination overflow and replace-required during imports: a response cursor
  never advances beyond delivered rows; rows and head share one snapshot;
- import finishing before SSE subscribe, disconnect/reconnect, adopted cold
  session and missed watch event: iOS/Rust recover through existing head/delta
  behavior. Lead 635 additionally requires the shipped PWA in these recovery
  controls, including cold partial/empty in-progress recognition through existing
  fields. Device execution is a release requirement routed by the lead; source
  checks alone do not prove it. No unrelated PWA work or cargo on this machine.

Seeded dropped-tail, wrong-carry, missing-delete, corrupted-summary, skipped-effect
and advanced-cursor mutants must fail the relevant oracle. For comparisons on
the live Host, retain revision 5's agent-wide message-head fence only after the
writer-completeness/relink proof, plus exact turns/authority inputs and same-file
frontier across every batch. Account for the import's own mutations; foreign
writes classify concurrent, not match/diverge. Synthetic quiet-state comparisons
do not validate an unfenced live audit. No global data_version fallback.

Measure fixed 1-10 KB appends on 1/10/17 MB histories and long single turns, at
least 10 warm appends per shape, plus restart/cold fallback. Include realistic
turn/client rows. Report parse/frontier, pure prelude, probe, raw validation,
locked write/maximum hold, effects, checkpoint, total, queries, bytes and peak
memory separately. The 250 ms p95 goal from lead 582 is an experiment target,
not a result. Earlier 7.7/9.3-second figures are full import totals; the 1.5 ms
RM-N10 validation witness is not an end-to-end forecast. If DB scans dominate,
report that before proposing P5. No weakened admission proof to meet a number.

## Rollout, evidence and closure

Each production package gets test-first controls, signed intended differences,
fresh exact G1/G2 and C; the captain owns exact CI/G4 and release owner owns G5.
Comparison filters must list only the signed timing/notification/effect-call
differences. Product-state equality remains mandatory. No schedule overrides
big merge 1: these are later runtime-class code releases at 18:00 under the
release owner's identity prediction and rollback policy.

Before live serve, repair or explicitly resolve every prerequisite in revision
5; 631 seeks fixes, not acceptance of RM-01/RM-02. Only the release owner can
activate observation/config. Retain the earlier seven-day observation proposal,
last-three-days zero divergence and >=500 valid comparisons per traffic-bearing
eligible backend, explained errors and <20% non-bootstrap fallbacks. Do not
borrow evidence across providers. Confirm the lead's observation schedule after
the new code is reviewed. Shadow metadata writes must be declared. CPU budget
needs measured worst-step cost/deadline and automatic off above 60 CPU-s/hour,
not an admission-only claim; memory/I/O and long fallback costs are reported.

After authoritative activation, keep full audits and report repairs/concurrent
skips separately. RM-02 rollout follows the prior design's three clean serving
days unless the lead changes that prerequisite. Preserve RM-N12 until its
replacement behavior is proven. Telemetry counts must include eventlog drops
from RM-N8b; mirrored table totals alone cannot certify no failures.

Close RM-01 only with serving evidence that ordinary supported transcript changes
avoid full-history work, provider coverage/fallback denominators and measured
costs. P1/P2 partial coverage does not close it for remaining providers or prove
strict DB incrementality. Close RM-02 only when every `/log` branch has zero
HTTP-thread imports, bounded response behavior, coherent read cursors and
background notification/recovery proof. Registers change on the lead's evidence
review, never because this document or the S5 move landed.

## Lead 635 decisions and remaining implementation gates

The plan/order are approved; C remains FINDINGS ONLY. A settled read is the
coherent committed snapshot above, and atomic whole-import publication is out of
scope. RM-02 closes only when every branch has no parse, store or import-lock
wait on the HTTP thread.

The 2.25-second cold FULL wait/known-slow skip and existing-type/payload completion
notifications are approved in direction. Exact intended differences are signed
later on P3's implementation commit. Cold responses must expose progress through
existing fields and all shipped iOS, desktop and PWA clients must recover complete
history on completion. A failed recognition/recovery proof blocks activation and
goes back to the lead. Reduced effect calls require a per-effect idempotence
proof and each effect named in the signed difference; no blanket exemption.

After P4, deliver measured residuals to the lead: O(history) raw DB validation,
restart loss of the frontier, and every backend retaining full fallback. Each
residual is fixed or taken by the lead to Peter as an explicit accept-or-fix
question. The lead does not accept a high-finding residual, and this plan grants
no acceptance. P5 remains unauthorized.

S5 coordinates P0/P3/P4; rf-bug-rmn12 retains P1 and its RM-N14 P2 frontier pieces
when explicitly resumed after Friday's ship. Claims go through the lead before
edits. Observation remains resource-capped. No source before big merge 1 ships;
no inclusion in Friday, Saturday or big merge 1. Review of this plan grants no
implementation, new stored format, live action or release authority.
