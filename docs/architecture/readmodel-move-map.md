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
| 4 | (this commit) | the `if role == "assistant" and not turn.get("id"):` block: the slot read and RM-N1b's parking of another request's occupant, RM-N1's `_shifted_twin` / `_parked_twin` move, the single matching `final:` row, `_streamed_twin`, and `_final_twins` adoption or removal | `_resolve_reply_id(database, *, agent_id, backend_session_id, source_file, seq, msg_id, text, timestamp, request_trace, reply_trace, final_rows, adopted_final_ids, placed_ids) -> (msg_id, changed)`; the caller ORs `changed` into `skipped_slot_removed` |

Step 5, the upsert, follows.
