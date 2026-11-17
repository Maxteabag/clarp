# Transcript import: live reply identity

Measured on 17 November 2026 against the Python Host after PR 405 at `bc9f0b2c`; the measured importer sources are identical on the final base. All inputs and databases are synthetic. No live Host, device or provider transport was exercised.

A transcript-first import can create a placeholder before a provider writes its live row. An occupied slot now adopts only a finalized, exactly equal, same-trace and unique live row, and only for the last parsed reply of its trace. Earlier replies retain main’s behavior. Unoccupied matching keeps main’s exact-before-prefix order, including streaming rows, with the deliberate exception that a row compatible with several parsed replies of its trace is declined. Running declined rows wait only for the last reply of their trace; later transcript records or turn settlement restore ordinary cleanup. Historical broken identities are not repaired.

The final live row keeps its original id, so references already attached to it remain usable. Finalization is per assistant message: Claude message-stop/new-message/result handling and Codex item.completed call mark_live_message_final, clearing kind in place. Finalized provider rows still use live:<trace>, not final:%; the finalization writer is unchanged.

## Candidate reads and cost

One lazy candidate statement family per writer hold serves occupied and unoccupied matching. It uses this batch’s traced assistant replies, chunks of 32 through idx_messages_trace, or the same session-walk threshold as PR 405: len(traces)>max(1,total_turns//5). There are no per-reply point lookups. Commits/yields drop candidates; own adoptions/deletions discard entries. Cleanup performs its own fresh read. No schema or index change.

The rich fixture carries 500 old turns, an authored client row and trace for every turn, and 228719 transcript bytes; the short fixture is 439 bytes. The earlier unlinked-history fixture hid an extra per-placeholder read and is excluded. A regression budget pins rich full-replay SELECT count. Runs use idle I/O priority and nice 19, serially. Counts include all SELECTs and SQLite VM operations during each call.

| Configuration | Old turns | Import | SELECTs main→change | VM main→change |
|---|---:|---|---|---|
| defaults | 0 | first-no-frontier | 36→36 | 1665→1538 |
| defaults | 0 | post-settlement | 40→39 | 2075→2282 |
| defaults | 0 | ordinary-tail | 34→21 | 1629→914 |
| defaults | 0 | full-replay | 16→15 | 527→493 |
| defaults | 500 | first-no-frontier | 5040→4545 | 6571363→3560323 |
| defaults | 500 | post-settlement | 6045→6050 | 342516→380928 |
| defaults | 500 | ordinary-tail | 34→22 | 167709→109924 |
| defaults | 500 | full-replay | 3016→3016 | 118519→125510 |
| HOSTS | 0 | first-no-frontier | 36→36 | 1667→1540 |
| HOSTS | 0 | post-settlement | 36→35 | 1843→2046 |
| HOSTS | 0 | ordinary-tail | 35→22 | 1641→926 |
| HOSTS | 0 | full-replay | 16→15 | 556→522 |
| HOSTS | 500 | first-no-frontier | 5042→4546 | 6571385→3560335 |
| HOSTS | 500 | post-settlement | 36→35 | 167843→168046 |
| HOSTS | 500 | ordinary-tail | 35→22 | 167641→109926 |
| HOSTS | 500 | full-replay | 3016→3016 | 118548→125539 |

first-no-frontier imports the transcript before the live row exists. post-settlement adopts the late finalized row; defaults may use a full path and HOSTS a tail. ordinary-tail is the following unchanged call; full-replay forces the full parser path. Transcript opens, reads and bytes are identical in each main/change pair. The rich initial import removes 495 SELECTs on defaults and 496 on HOSTS by eliminating the per-reply session walk. Full replay stays at 3016 SELECTs. One measured unchanged defaults call includes an additional settings-cache SELECT; the cleanup restamp hook itself performs no reads without a deletion/adoption.

| Read | Main plan | Change plan |
|---|---|---|
| Cleanup, bounded | idx_messages_trace(trace_id=?) | same |
| Cleanup, fallback | idx_messages_agent_revision(agent_id=?,backend_session_id=?) | same |
| Legacy finalized rows | idx_messages_final_rows(agent_id=?,backend_session_id=?) | same; SQL unchanged |
| Client provenance | same non-covering indexed read plus temporary ORDER BY | same |

Cleanup adds only the flag (turn_json IS NOT NULL), not the summary JSON. The accepted provenance projection adds trace_id to the existing read. On 501 rows the cost was 14,602 → 15,604 VM operations (+2 per row), with no new table visit or SELECT.

## Summary placement and recovery

Main stamps before cleanup in one shared place, _store_transcript_turns_txn, used by both full and tail import APIs in their probe and locked passes. With late live rows, that stamp can select a row cleanup then removes, even without adoption: completed two-live, remark-only, non-exact declines, and interrupted turns can lose the summary. Good-order fixtures keep it on the final surviving reply. Lifecycle restamp_turn also stamps but does not itself clean rows.

A single post-cleanup hook now calls the unchanged stamp for traces with an occupied-slot adoption, or whose stamped live row cleanup actually deleted. Every other trace is stamped where main stamps it. Five main comparison cases all finish with 2 assistant rows and 1 summary on the surviving final reply. The expanded matrix also covers 3 messages, non-exact final declines, both orders and interrupted outcomes. All 48 cases keep message rows, heads and the message clock unchanged through identical, no-frontier full and reopened-cache replays.

The conformance recording with a surviving final live id is covered too. Both tool-turn fixtures take `what=row_live,turn_of_old_row` followed by `full_carry_inputs_changed`. One already has an occupied final slot; the other adds the final after the frontier so its live id survives even on main. The upsert does not clear turn_json: the pre-cleanup stamp moves it to an earlier live remark whose later timestamp sorts last, and cleanup deletes that remark. The existing deleted-stamped-live trigger repairs the surviving final; no broader stamp trigger is needed. Both tests fail on main and pass on this change. Occupied-adoption bookkeeping is confined to slots that actually existed.

If a forced writer yield commits cleanup’s DELETE and an exception prevents the post-cleanup stamp, the turn temporarily has no summary. The next explicit full import repairs it. An admitted tail carrying that trace as newest also repairs it, including an empty admitted tail; a fast no-op which never enters the writer does not, and an unrelated trace is not a repair promise. Without the forced yield, an exception rolls back the cleanup transaction. Six actual TailWriter controls cover these windows. Untouched main also repairs its initially lost summary on the next explicit full import; this change repairs it in the adopting/cleaning import itself.

Stated cost: the two-live adopting import adds 2 SELECTs, 2 UPDATEs and 1 head INSERT relative to the identity-only candidate. A no-leftover full replay adds zero statements or VM operations relative to that candidate. Publication policy and the stamp function are unchanged.

## Revisions, lanes and pointers

| Adopting import | Main allocations | Change allocations | Intermediate head-only allocations |
|---|---:|---:|---:|
| First turn, 8 R19-order matrix cases |1|2|0|
| First turn, 2 one-row controls |1|2|0|
| Earlier turn, scenario 2, one-row batches |6|23|15|

The 18-case allocation probe confirms the first-turn figures. The scenario 2 head ends at 32 on main and 49 with this change. The 17 extra allocations comprise one adopted payload, its summary, and 15 head-only advances. No downstream row is rewritten only because adoption moved a slot; eleven existing downstream rows are unchanged. A common summary changes on both versions. The post-cleanup hook adds no allocation in this fixture.

Stated limit: adoption of an earlier turn’s last reply during a full import advances the conversation replace cursor once per later commit batch (existing policy; head only, no row re-sync). A client reloading on every replace advance would reload that often. Publication optimization is separate work.

The six named HOSTS scenarios remain 96/102 at main’s outcome and the same 6 scenario 2 identity/revision changes; no decision, carry outcome or row-count changes. Only that test’s outcome line changes: finalized live-left owns transcript seq 8 and the placeholder is retired. Its prompt, trace and carry assertions remain. The chat-row streaming-duplicate regression now checks both correct-trace rows while the last reply still streams, then finalizes it and checks the single retained live id. Its queued-send/trace reproduction remains.

Two independent 12-replay sets across Claude/Codex and defaults/HOSTS keep every message column, head and clock byte-identical: identical import, full without frontier, full after isolated-worker restart. The added set supplies both a late earlier live remark and late final live row in an old trace; the final live id holds its sole summary. This is worker-restart evidence, not a live Host restart.

| Pointer or reader | Result |
|---|---|
| Notification source message id | Original live id remains resolvable; notification reference covered |
| Retained audio by reply/trace | Original reply id retains lookup; synthetic clip/queue covered, no speaker/device proof |
| History cursor and replacement delta | Retained live id usable; retired placeholder removed through existing replace cursor |
| Turn summary | One summary on final surviving reply, including interrupted and remark-only cases |
| Oracle result id, team source id | Resolver returns adopted id; this race not independently reproduced for these consumers |
| Artifact/decision source ids | No historical pointer rewrite; this race not independently reproduced |

## Carried trace consumers

- message_import_common._client_provenance_rows: produces _ClientProvenanceRow with original five positions plus named trace_id from the same SELECT. Same non-covering plan. On 501 rows the cost rose from 14,602 to 15,604 VM operations (+2 per row).
- message_import_common._session_parts: derived producer copies trace_id from the full rows it already has, same order and original positions, no query.
- message_import_common._client_provenance: ignores appended trace; slices original five inputs and emits exactly the existing four-field normalized tuples.
- message_import_rows._ClientProvenance: receives that unchanged four-field mapping; candidates/take/resume keep their ordering and consumption semantics.
- message_writes._sends_and_latest_user/_prepare_import_prelude/_revalidate_import_prelude: retain the named raw rows; existing equality/revalidation rules include the carried field, while normalized choices and values remain unchanged.
- message_writes._ImportPrelude.for_pass/raw_carry_inputs: copies or forwards the raw list. message_carry.record/changed unpack that second input as _sends and ignore it; carry products/decisions do not gain a new positional dependency.
- message_import_rows._FinalRows/_parsed_traces: use named message_id/trace_id to prepare traced reply ownership and the batch reader membership. The provenance mapping is copied before take, so the writer's pending rows are not consumed.
- message_import_rows._own_turns_row: its existing main caller keeps the default database path unchanged. The new read-only lookahead caller supplies the carried trace map and reuses the same selector without SQL. Original normalized row indices remain unchanged.

The direct/derived named-field test pins the five original positions, named trace and unchanged normalized mapping. Existing import-session-parts randomized/derived/revalidation tests and authored-time-index boundary tests pass. No wire field, schema or finalization-writer change.

## Diagnostics and verification

Log events: transcriptLiveTwinOtherTurn is retired everywhere. The scoped reader cannot see another turn’s row, so it can neither adopt nor report it; a no-trace reply returns before SQL or diagnostics as main does. transcriptLiveDeclineGuardFail is new and content-free (reason/count), with failure-safe diagnostic coverage. transcriptDiagnosticFallback is unchanged. No other log event is added or changed.

Daytime verification passed with 336 focused checks, 303 named HOSTS cases, 17 integration checks, 908 critical tests and 53 guards. Critical also reported 7 skipped, 199 deselected and 1 expected failure. New summary matrix failed 24 bad-order cases before the repair while 24 good-order controls passed. The unmodified control passed. Of 36 mutants, 35 failed on the first run. One retained an ambiguous live row for two earlier replies even though the final reply was unrelated. A new regression passed on the unmodified code and failed on that mutant. All 36 mutants now have a failing test receipt. The final run passed 337 focused checks, 908 critical tests and 53 guards (all exit 0). The whole importer lane in both configurations is Arnold’s separate night gate on the PR merged with main. No live activation claimed.

All three changed production modules are hashed into the importer parser fingerprint: installation costs one full import per conversation. No deliberate parser-version constant, wire feature or client-contract change.
