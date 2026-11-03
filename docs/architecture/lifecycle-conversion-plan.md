# Lifecycle conversions: the Phase 3 plan

Status: proposal for reviewer A and the lead, 3 Nov 2026. Owner: the contract
owner (`rf-contract`). It plans the first four conversions from the table in
[lifecycles.md](lifecycles.md#lifecycles-to-convert-phase-3), which says what
the kit is and what each lifecycle breaks. This page says in which order they
convert, what each conversion changes, and what has to be decided first.

The facts below are from main at `a78affb9`. File and line references are to
that commit.

## Order

| # | Lifecycle | Why here |
|---|---|---|
| 1 | Queue rows (`queued_turns.status`) | It is the pilot. It is already almost compare-and-set, with one module and one table. Turning that into the kit proves the reaper host, the cross-process pattern and the guard bookkeeping at the lowest risk. |
| 2 | Dream runs (`dream_runs`, `dream_rounds`) | It has the least protection today: no transaction, blind writes, a possible double send and AUT-06's endless re-queue. There are few callers and nothing user-facing waits on it. |
| 3 | Goals (`task_plans`, `task_items`, `agent_goals`) | It is mostly compare-and-set already, but three kinds of process write it. AUT-01 and AUT-N1 are fixed; their follow-ups (below) belong in this conversion. |
| 4 | Turns (`state_log.kind`, `turns`, slots) | It is the most severe (TP-01/10/11) and also the widest. About 40 call sites in 12 modules write it, from at least three kinds of process (runtime, server, each Claude hook). It is runtime-class (18:00 releases only) and overlaps S4. It converts inside or right after the S4 turn engine slice, on its seams, not before them. |

Severity alone would put turns first. The order is chosen so that turns, the
conversion that can do the most damage, is made last, with the kit already
proven by three conversions and the S4 seams in place. The queue conversion
also removes most of the turn-to-queue coupling that turns would otherwise
carry: `settle_receipt`, `mark_started` and the stale-claim reset.

## Rules for every conversion

- **One slice per lifecycle, in this order of commits:**
  1. G0 characterization tests of today's transitions, on the old code.
  2. Declare the `Lifecycle` constant in the owner module. Nothing calls it
     yet, and the guard tests take it in (the column leaves `UNCONVERTED`).
  3. Route each writer through `lk.transition` / `lk.compare_and_set`, one
     writer per commit where the change is not trivial.
  4. Move the effects into the outbox.
  5. Add the reaper.
  6. Remove the `STATUS_WRITERS` entries.

  Steps 3 to 5 change behaviour where the old code was blind or had no
  deadline. Each such change is a registered intended difference (G2), named
  before the run.
- **One owner is one module, not one process.** SQLite compare-and-set works
  across processes, so a hook subprocess or the agent CLI may call the owner's
  functions. What they may not do is write the column themselves. The table
  writers guard already enforces this per module.
- **The reaper host.** One server tick runs `lk.reap` for every database
  lifecycle, each inside its own `lk.transaction`, and queues the reaped
  transitions' effects in the outbox. The candidate is the scheduler loop that
  already runs the goal recovery tick. In-memory lifecycles (slots, the Live
  phase) reap in their owner, under the owner's lock. The reaper is added to
  the program's hot-path watch: it must not repeat the 09:14 lock incident
  (AUT-N1). It touches only the rows it moves, it reads through an index on
  `(state, entered_at)`, and it never rewrites a whole document.
- **Indexes are schema changes.** A reaper needs `entered_at` and an index on
  each table it scans. That is a migration, so it is a contract-owner commit
  (`db_schema.py`, `db_migrations.py`, goldens and the rehearsal). Phase 1
  forbade it; Phase 3 needs the lead's go per table. The S1 end window
  (moving `db_schema.py`) must not overlap a conversion's migration.
- **The client contract is frozen.** State values that reach a client (queue
  receipts, goal status, the agent's state) keep their spelling. A
  conversion may add a state only when no client sees it, or when it bumps
  `HOST_CONTRACT` with a row in `docs/compatibility.md`, which is a lead
  decision.
- **Replay.** Each conversion needs scenarios that reach its states.
  Existing: `queue-while-busy`, `queue-cancel`, `stop-with-queue`,
  `goal-turn`, `stop-mid-turn`, `preempt-live-process`. Missing: a dream round
  and a goal recovery tick. Those are asked of the harness owner before the
  conversion starts.

## 1. Queue rows

**Today** (`turn_queue.py`). The states are queued, claimed, parked and
started, then done, failed, interrupted, ended, unknown, cancelled and
withdrawn. Almost every write is already compare-and-set
(`WHERE status IN (...)`). `BEGIN IMMEDIATE` wraps `move_to_back`, the
terminalizers, `remove`, `cancel`, `update_text`, `remove_for_agent` and
`set_paused`. Both the server and the runtime write; there are about 67 call
sites, 37 of them in `turn_dispatch`. There are no effects inside the module.
These reapers exist:
- `reset_stale_claims`: claimed after 30 s goes back to queued.
- `terminalize_ended_receipts`: at startup.
- `terminalize_idle_agent_receipts`: 24 h.

**Breaks the kit:**
- `settle_receipt` (:280) lets a newer settlement overwrite a settled
  receipt ("a same-trace retry that later succeeds turns `failed` into
  `done`"). That is an edge out of a final state.
- queued and parked rows never expire (:445).
- `_move_behind` (:86) deletes and reinserts.
- `allow_paused_for` (:621) writes a flag without a guard.

**Decision needed (kit, reviewer A).** The receipt's settled states must stay
spelled as they are, since clients read them. There are two ways to fit
"newest settlement wins":
- **(a, recommended)** Add correction edges to the kit:
  `Edge(..., correction=True)`. It is allowed only between final states,
  needs no deadline and is compare-and-set like any edge. Validation rejects
  a correction that leaves the final set. It is small and explicit, and it
  documents the one place in the program that rewrites a final state.
- **(b)** Make done, failed, interrupted and ended non-final with a deadline
  that expires into themselves. That is rejected: it hides the real rule
  behind a fake wait.

**Waits:**
- claimed: `Deadline(30 s, expire_to=queued)`. The deadline goes back to a
  non-final state, so the kit must allow `expire_to` a non-final state. That
  is a second kit change. Today's `reset_stale_claims` already does it, so
  this is no behaviour change.
- started: `WaitForever("the turn's settlement ends it; the idle sweep after
  24 h is the backstop")`. Or `Deadline(24 h, ended)`, which equals today's
  `terminalize_idle_agent_receipts`. Recommended: the deadline, because it
  is the same behaviour declared.
- queued: `WaitForever("dispatch, cancel or the agent's deletion ends it")`.
- parked: `WaitForever("the user resumes or cancels the paused queue")`.

  Both of these are honest waits on a person or on dispatch. A queued row of a
  deleted agent is ended by `remove_for_agent`, which is already the case.

**Commits:**
1. Kit: correction edges and a non-final `expire_to`, with tests.
2. Declare `QUEUE_RECEIPT`.
3. Route each writer through the kit.
4. `_move_behind` becomes an `UPDATE` of the position, not a
   delete-and-insert, if the position column allows it. Otherwise it stays
   one transaction and is named in the declaration's notes.
5. The reaper replaces `reset_stale_claims` and the idle sweep (the same
   numbers).

**Behaviour changes to register:** none intended. The deadlines are today's
values. If commit 4 changes the order of equal positions, that is registered.

## 2. Dream runs

**Today** (`dreaming.py`, 2,273 lines). The states:
- `dream_runs.status`: active, then completed, abandoned or noop (final).
  `stage` is free text.
- `dream_rounds`: queued, sent, then completed or abandoned. sent can go back
  to queued.

There is no transaction anywhere in the module. Writes are blind except
`_recover_stale_sent_round` (:1082) and the round update in `abandon_run`
(:871). Two processes write: `process_assistant_text` arrives from the runtime
(`turn_dispatch.py:3565`) and the scheduler runs in the server
(`server.py:7142`).

**Breaks the kit:**
- `_send_dream` dispatches the turn (:2203) before `mark_round_sent` (:2209),
  and both the poll loop and the `request_advance` threads (:2237) reach
  `_advance_run`. One round can be sent twice (property 4, and 2).
- A sent round goes back to queued after 90 minutes with no attempt cap, so a
  failing round is re-queued forever and blocks later dreams (AUT-06,
  property 3).
- An active run has no deadline. It stays active while the agent is always
  busy, or when synthesize ends through the stage marker without a digest
  (`_ensure_next_rounds` :1534).

**Declarations:**
- `DREAM_ROUND`:
  - queued: `Deadline(24 h, abandoned)`, a round the scheduler never managed
    to send.
  - sent: `Deadline(90 min, queued)`, as today, plus an attempt cap.
  - When the cap (proposed: 3) is reached, the deadline goes to abandoned
    instead. The kit's deadline has one target, so the cap is a decision in
    the reaper's caller: it reads `attempts` and fires `abandon` rather than
    `deadline_expired`. That stays inside the kit, because the decision is
    made in the same transaction.
- `DREAM_RUN`: active: `Deadline(48 h, abandoned)`.

**Commits:**
1. Order fix: claim the round (queued to sent, compare-and-set) in a
   transaction. Dispatch the turn from the outbox, so only the thread that
   won the claim sends it.
2. Route the remaining writers through the kit.
3. Add the reaper with the attempt cap and the run deadline.
4. Move `_deliver_digest` and `harvest_worktree` into the outbox after the
   final write.

**Behaviour changes to register:**
- A round that fails three times is abandoned, not re-queued. This fixes
  AUT-06.
- A run that is active for more than 48 h is abandoned.
- A double send is no longer possible. A replay of a dream round shows one
  dispatch where the base can show two.

These need the lead's classification as fixes inside a conversion. They need
the missing dream-round replay scenario.

## 3. Goals

**Today:**
- `task_plans.status`: active, paused and blocked; final: completed,
  cancelled and superseded.
- `task_items.status`: pending, in_progress, blocked; final: completed,
  skipped, deferred and removed.
- `agent_goals.status`: a mirror of a native Codex goal, written by
  `codex_app_server.py:402`.
- `goal_json.continuation.state`: a second state machine of 13 values inside
  the document.

Most writes run in `BEGIN IMMEDIATE`. `finish`, `_auto_finish` and
`_goal_save_if_unchanged` are compare-and-set. Writers run in the server, the
runtime (`backend/agy.py:251`) and the agent CLI (`scripts/agent_tasks.py`).

**Breaks the kit:**
- `cancel_for_agent` (:317) is blind and autocommit. It is called from
  `agents.soft_delete`.
- `_goal_mutate` (:708), `_goal_save` (:449) and recovery's `_result`
  fallback (`task_goal_recovery.py:432`) write the status blind. They do so
  inside a transaction, but after a revision check, not a state check.
- These states have no expiry: paused, blocked, attention and not_enrolled,
  legacy active plans with no continuation, and `in_progress` items.
- A stale native `agent_goals` row blocks Host goals (AUT-20).

**The AUT-N1 and AUT-01 follow-ups belong here:**
- **Bound `goal_json` in bytes, not only in entries.** History is capped at
  200 entries (`GOAL_HISTORY_LIMIT`), but a replan appends `prior_steps` with
  up to 500 full item rows (:840), and the legacy `history_json`
  (:529-535) has no cap at all. A cap in bytes, plus moving `prior_steps` to
  the append-only `goal_ledger`, keeps every rewrite small. The 09:14
  incident was a large rewrite.
- **Decide inside the transaction.** Several things read outside the lock
  and decide on what they read:
  - recovery's `_claim` reads the plan and `boundary()` outside it
    (`task_goal_recovery.py:228-275`);
  - so do the first `_result` attempts (:409), `validate_dispatch` (:105),
    `allows_paused_queue` (:440) and `pause_recovery_for_agent` (:945).

  The conversion moves each decision into `lk.transaction`. Where the input is
  a large document, it uses `json_extract` on the needed fields, not a full
  parse.
- **Two lifecycles, not one.** `task_plans.status` is the plan lifecycle. The
  continuation state is the recovery lifecycle of the same row. The
  conversion declares both. The continuation's state moves out of the
  document into a column (a schema change, see the rules), so it can be
  compared and set and reaped without rewriting `goal_json`.

**Waits:**
- plan, active: `WaitForever("the agent works it; recovery's own deadlines
  bound each attempt")`.
- plan, paused: `WaitForever("the user resumes it")`.
- plan, blocked: `WaitForever("a person unblocks it")`. The reason must name
  who, and the UI already shows blocked plans.
- recovery:
  - dispatching, admitted and queued: `Deadline(60 s lease)` as today.
  - retry: `Deadline(backoff)`.
  - attention: `WaitForever("the user")`.
- `agent_goals`: active `Deadline(native heartbeat age, blocked)`. This fixes
  AUT-20. The value comes from the Codex goal's own update cadence.

**Commits:**
1. Byte bound and `prior_steps` to the ledger. This is a fix-lane commit
   with its own test, and it can land before the conversion.
2. Continuation state into a column (a migration).
3. Declare the plan and recovery lifecycles.
4. Make `cancel_for_agent` a compare-and-set inside a transaction.
5. Move the decisions into transactions.
6. Add the reaper, and the native-goal deadline for AUT-20.

**Behaviour changes to register:**
- A stale native goal stops blocking (AUT-20).
- A cancel racing a finish is a `Conflict`, not an overwrite.
- Larger plans keep a shorter inline history.

## 4. Turns

**Today:**
- `state_log.kind` holds `AgentState`: spawned, thinking, tool, compacting,
  waiting and background are non-final; done, idle, interrupted and stopped
  are final.
- `turns` uses `ended_at`, `settled_at` and `outcome`.
- The slots are in memory under `OwnershipLock`.

The default writer `transition()` (`turn_lifecycle.py:301`) reads and then
inserts in separate autocommit statements, and `force=True` skips the check.
`_conditional_transition()` (:360) is the one compare-and-set. `_settle_turn`
(:192) writes the outcome, restamps the messages and settles the queue
receipt in three autocommit statements. The process is spawned
(`turn_dispatch.py:2855`) before `SPAWN_STARTED` is written. There is no
reaper for waiting, spawned or background without a custom status.

**Why last, and how:**
- The state is an append-only log (`state_log`), not a row. Compare-and-set
  means "insert only if the newest `state_id` is the one I read", which is
  what `_conditional_transition` already does. The kit needs a log adapter
  for that: compare-and-set on the newest row and insert a new one, rather
  than `UPDATE`. This is the third kit change, and it is designed with S4.
- Every Claude hook is a writer process (`plugin/hooks/*.py` through
  `hook_transition`, :701). Hook writes become events to the owner's
  functions, which they already call. They must become conditional, so a
  late hook cannot move a settled turn.
- Settlement becomes one transaction: outcome, restamp and receipt (the
  queue's settle edge from conversion 1).
- The spawn moves into the outbox of the `spawn` transition: decide, commit
  SPAWN_STARTED, then start the process. Preemption waits for the old
  process's exit event before the new spawn effect runs (TP-01). This is the
  S4 turn engine's work. The conversion supplies the declaration and the
  guard, and S4 supplies the seam.
- Waits:
  - spawned: `Deadline(spawn grace, interrupted)`, today's orphan-spawn grace
    (`turn_dispatch.py:3440`).
  - waiting: `WaitForever("a person answers the prompt")`.
  - thinking, tool and compacting: `WaitForever("the process is alive; its
    exit ends the turn")`, with the process lifecycle's own deadline as the
    backstop.
  - background: `Deadline(stale_work age, idle)`, as `BACKGROUND_EXPIRED`
    does today.

**Behaviour changes to register:**
- A late hook can no longer overwrite a settled state.
- `turns.ended_at` is set on normal completion (TP-11).
- The next process starts only after the old one exits (TP-01). The
  `preempt-live-process` scenario already reaches this.

## Kit changes this plan needs

These are made by the contract owner and reviewed by A, before the
conversion that first needs them:

1. **Correction edges between final states** (queue). Validation keeps them
   inside the final set.
2. **`Deadline.expire_to` a non-final state** (queue claims, dream rounds).
   The reachability and "can reach a final state" checks still apply.
3. **A log-shaped lifecycle** (turns): compare-and-set on the newest row of
   an append-only table.

Nothing else changes in the API.

## Open questions for the lead

1. The order above (queue, dream, goals, turns), with turns tied to S4?
2. Phase 3 needs migrations: `entered_at` columns, reaper indexes and the
   goal continuation column. May the contract owner make them, one per
   conversion, after the S1 end window?
3. The dream-round attempt cap (3) and run deadline (48 h), and the AUT-20
   heartbeat deadline. These are behaviour numbers that need the owner's
   values or the lead's.
4. The goal follow-up commit 1 (byte bound, `prior_steps` to the ledger) can
   go through the fix lane now. Should it?
