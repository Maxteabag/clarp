# Populated replay pass proof — source slice, not a capacity certificate

Exact A4f33 and B007 source homes/symbol ranges/AST hashes are frozen in
`populated_replay_pass_sources.json`. No importer, parser, SQLite, helper or
control has run. C closed only the a96cedb2 checkpoint-policy source finding;
actual seed/policy/loaded-code/lifecycle evidence is still absent.

## Native paths that must not be conflated

| Conditional operation | Native behavior that matters to observation |
| --- | --- |
| Current A INITIAL, fresh default-disabled | Full parse/store, no shadow callback, zero transcript CP per fixture owner. Physical WAL close is separate. |
| A populated replay, disabled, with all native row/summary/head/cleanup predicates unchanged | The deferred probe reads one snapshot and returns after COMMIT without locked replay. No callback is supplied by this A importer route. This condition is not yet established for actual cells. |
| B disabled native import, no process-local frontier | `_try_tail` returns `no_frontier` before loading a durable CP. It is wrong to call the first decision `decide(no_checkpoint)` merely because the restored A image has no CP. |
| B full bootstrap, qualified boundary/carry/file/producer conditions | `_store_full_with_tail_checkpoint` supplies `on_stored`; it may publish a CP even when every message row is unchanged. Its publication is conditional, not guaranteed by a full-import return. |
| B already-certified process-local frontier | Durable CP equality, file/byte/hash/carry/age checks precede tail application. Another process's durable CP is not this process's frontier. No CP or in-memory state is manufactured in the measurement package. |
| Either pin, message/summary/head/cleanup write needed | `_WriteNeeded` is caught by native store code; the deferred probe rolls back, then raw-prelude revalidation and locked replay run. Already committed batches must remain visible to the final oracle. |

In both `_write_checkpoint` implementations, `on_stored` receives the REAL
database connection, not `_ImportWriter`. A read-only probe may therefore
upgrade its deferred transaction behind `SAVEPOINT transcript_checkpoint` and
write a CP without `_WriteNeeded`/locked replay. A CP write is neither a
message-clock allocation nor proof the importer took its locked row path.
The actual native CP values, publication outcome and transaction still need
their own evidence; the current revision-keyed component is not a CP oracle.

An exception inside the callback is caught by native `_write_checkpoint`, which
rolls back to/releases its savepoint and logs the skip. The outer import can
still return successfully. Thus a disabled/full return does NOT prove required
publication or absence of attempted/discarded work. Normal retry/fallback,
unresolved-history censor and native in-flight/drain domains remain distinct.
Do not reclassify a callback failure as harmless by looking only at final rows.

## Small boundary shapes, strictly conditional and connection-local

For an actual unchanged A probe with no callback, no exception, no other work
on that same native connection and a complete f628 witness, the selected
transaction markers are one `transaction_begin` and one committed `boundary`.
For an otherwise unchanged B probe with exactly one successful native CP
savepoint, the selected markers additionally include `savepoint_begin` and
`savepoint_release`. These are two/four **selected boundary emissions**, not a
complete operation/actor/bank bound, measured observations or a qualified choice
of8192/512. CP field evidence, aliases, connection/operation bookkeeping,
observer cost, raw capture and all other native actors are not counted by that
statement. It cannot be multiplied by a guessed import rate to certify fit.

Locked replay is different. Its batch admission/commit/yield/reacquisition can
depend on native elapsed writer-hold time as well as sequence distance.
Unchanged payload counts do not remove those epochs once a write caused replay.
Append, restamp, retained-revision moves, deletion/parking/adoption, head changes,
CP savepoints and failed/unknown outcomes need their actual keyed/epoch linkage.
No elapsed-time-based batch bound is inferred from IMPORT_COMMIT_EVERY alone.

## Row replay conditions that remain to be established

The exact `claude_transcript.parse_turns` and `_row_phases` ASTs compare equal
between A and B. This is a limited source fact, not a native output certificate.
The declared recipe contains plain user strings and one assistant text block,
no isMeta/synthetic-error/tool/subagent records. The parser does not copy its
record UUID into an `id` key in the returned plain turn. Consequently the real
client-provenance and reply-resolution branches, rather than assumed UUID
identity, remain relevant to the seed/replay proof.

For each actual seed owner/session, the still-owed raw predicate check must bind:

- exact transcript bytes/path/sequence and actual parsed role/time/text/tools/
  cells/kind/phase, without calling a copied parser as a substitute for native;
- all durable client rows and their text/order/origin/sender/trace matching,
  consumed-client membership and retry-echo decisions;
- actual turn membership/start/settle/outcome/trace-at selection, including the
  pre-seeded future six turns and every other row/session that can affect a
  summary or ambiguity decision;
- native canonical reply IDs, exact/final/shifted/parked candidate absence or
  uniqueness, all current source/seq values and placed/adopted membership;
- final summaries, last-row ordering/ties, tool/cell settlement, phase and raw
  payload equality. Matching SELECT/expression ASTs do not prove the rows they
  read are equal or the same last row was selected;
- exact live/stale/parked/authority/delegation inputs, head revision/replace
  coverage, and every direct/startup/recovery writer on the shared database.

Each predicate must come from the qualified native closed image/reference and
native actor/source/operation evidence, not a JSON True flag. Initial construction
has no recorder today but its complete raw work and storage remain charged.
An empty-first independent reference needs its OWN event/resource bound; the
populated probe shape above cannot cover reconstruction by assertion.

No new recorder infrastructure, capacities, fixture reduction, source/CP/time
rewrite, controls, compute or metric verdict is introduced here. Whole-package
qualification and LOWEST-priority compute/captain admission remain required.
