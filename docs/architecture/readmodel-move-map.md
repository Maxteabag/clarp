# S5 move map (read model)

Refactor slice S5 splits `message_writes._store_transcript_turns_txn` into named
parts (read-model review section 3, step 4). This file lists every move, so a
branch that touched the old code can find where each piece went. It is updated
with every S5 commit that moves code. Base: `origin/main` 9297ea75 (the
function is at `server/lib/message_writes.py:986`, 508 lines).

## Import paths

None change. Every name below is new and private to `server/lib/message_writes.py`.
`message_store` re-exports nothing new. No caller outside the module changes.

## Steps 1-3 (extractions; refactor commits, not move-only)

| Step | Commit | Old code (inside `_store_transcript_turns_txn`, at 9297ea75) | New home |
|---|---|---|---|
| 1 | 76f59614 | `client_user_provenance` dict and its `SELECT … source_file LIKE 'client:%'` load (lines 1015-1032); `consumed_client_ids`; the carry filter (1045-1052); the lookup `client_user_provenance.get(key) or []` (1225); the take `rows.pop(0)` / `consumed_client_ids.append` / `client_user_provenance.pop(key)` (1236-1245) | `_ClientProvenance`: `__init__` (load), `resume(consumed_ids)`, `candidates(key)`, `take(key, rows)`, attribute `consumed` |
| 2 | 4e9af430 | `assistant_ordinal`, `authority_tools`, `closed_authority` and their carry restore (1056-1062, 1072-1075); `closed_authority.update(authority_tools)` on a user turn (1135); the `agy_turn_authority` lookup and `assistant_ordinal += 1` (1139-1148); the collect into `authority_tools` (1161-1166) | `_AgyAuthority`: `resume(carry)`, `close_open()`, `lookup(database, agent_id, bsid)`, `collect(authority, turn)`, attributes `ordinal`, `tools`, `closed` |
| 3 | 1205dd69 | everything after the turn re-stamp loop: leftover `live:` twins, the stale-tail delete, the replace revision, `_attach_authority_tools`, the head read and upsert (1423-1493) | `_finish_import(database, agent_id, bsid, *, source_file, batch_finals, total_turns, skipped_slot_removed, latest_revision, authority_tools)` (`source_file` since merge 13: RM-N1c's `_restore_unadopted_parked` runs there) |

The carry keys written by `capture()` are unchanged (`consumed_client_ids`,
`assistant_ordinal`, `authority_tools`, `closed_authority`, …). They now read
`provenance.consumed`, `agy.ordinal`, `agy.tools` and `sorted(agy.closed)`.
Stored checkpoints stay readable, and the checkpoint guard proves it.

## Rebasing a branch over these commits

No tool is needed, because nothing is renamed outside the function. A
conflict inside `_store_transcript_turns_txn` means your change touched one of
the blocks above. Re-apply it in the new home from the table. The behaviour
must stay the same: rerun `tests/readmodel/` (byte goldens, characterization,
incremental goldens, checkpoint guard) one file at a time, and your own tests.
The fix lane's RM-N1 edits the upsert and twin path, which steps 1-3 do not
touch.

## Steps 4-5 (after RM-N1 landed; base refactor/integration 0e6aa676)

| Step | Commit | Old code (inside `_store_transcript_turns_txn`) | New home |
|---|---|---|---|
| 4 | 7937155d | the `if role == "assistant" and not turn.get("id"):` block: the slot read and RM-N1b's parking of another request's occupant, RM-N1's `_shifted_twin` / `_parked_twin` move, the single matching `final:` row, `_streamed_twin`, and `_final_twins` adoption or removal | `_resolve_reply_id(database, *, agent_id, backend_session_id, source_file, seq, msg_id, text, timestamp, request_trace, reply_trace, final_rows, adopted_final_ids, placed_ids) -> (msg_id, changed)`; the caller ORs `changed` into `skipped_slot_removed` |
| 5 | (this commit) | from `timestamp = turn.get("timestamp")` to the `final_rows` bookkeeping: the existing-row read, the row trace and settled tool statuses, the unchanged check, revision and `updated_at`, RM-N1b's park before a moving rewrite, and the `INSERT … ON CONFLICT(message_id)` | `_upsert_row(database, *, agent_id, backend_session_id, source_file, seq, msg_id, turn, role, text, origin, sender_agent_id, request_trace, phase, turn_index, final_rows) -> _Upserted(timestamp, tools, display_cells, row_trace, unchanged, revision, changed)`; the caller keeps `newest_trace`, `touched_traces` and `latest_revision` |

What stays in the loop: the per-turn classification (user, assistant, tool rows; ack tokens; client-row linking via `_ClientProvenance`; agy authority via `_AgyAuthority`), the heartbeat accounting and the team/oracle captures after the upsert, and the output list.


## Module-size cut after merge16 (lead603/604)

Base `a7331017` has the reviewed RM-N10 prelude. Exact named moves and AST
fingerprints are in [s5-message-split.json](s5-message-split.json); ownership and
test patching use `tools/refactor/handwritten/s5-message.toml`. These modules
are hand-written and join the captain's forward-port watch; no generator owns
or regenerates them.

| Canonical module | Responsibility |
|---|---|
| `message_import_common` | IDs, revision increment, row/provenance normalization, timestamp/retry helpers and scalar matching constants |
| `message_import_rows` | client provenance, agy authority, final-row cache, parking/twins and row upsert |
| `message_writes` | public full/tail/planning entrypoints, writer admission/batching, pure phases, exact RM-N10 prelude revalidation and finish orchestration |

`_visible_replies`, `_finish_import`, `stamp_turn`, `turn_stamp` and
`_attach_authority_tools` stay in `message_writes`; `_FinalRows` is canonical in
`message_import_rows`. The `message_store` facade (including
`dashboard_messages`) is unchanged; `conn` and `IMPORT_*` are still resolved at
call time by the existing orchestrator. Helpers use its supplied writer and
connection, preserving reads, decisions, writes, commits, yields and side effects.
Both new modules enter `transcript_incremental._COMMON_SOURCES` so old parser
versions still invalidate checkpoints. Existing checkpoint/epoch guards stay.

G0 preceded every source move: original-D tests pass (194); the additional
Codex collaboration characterization passes (23). Identical-source coverage
union: rows 202/209 (96.65%), common 38/38 (100%), Codex cells 589/676 (87.13%).
The three moved constant declarations are also covered. No golden was changed;
fixtures use the existing family-aware `patch_shared` helper for clocks and
fault injection. This cut changes module boundaries, with no intended behavior
difference. RM-N14's later tail-import work retains the orchestrator and its
atomicity; it is not implemented by this split.
