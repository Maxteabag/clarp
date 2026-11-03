# Lifecycle conversions: the Phase 3 plan

Status: proposal for reviewer A and the lead, 3 Nov 2026. Owner: the contract
owner (`rf-contract`). It plans the first four conversions from the table in
[lifecycles.md](lifecycles.md#lifecycles-to-convert-phase-3), which says what
the kit is and what each lifecycle breaks. This page covers three things:
- the order the four conversions happen in;
- what each conversion changes;
- what has to be decided or built in the kit first.

The facts below are from main at `a78affb9`. File and line references are to
that commit.

## Order

| # | Lifecycle | Why here |
|---|---|---|
| 1 | Queue rows (`queued_turns.status`) | It is the pilot. It is already almost compare-and-set, with one module and one table. Turning it into the kit proves the bounded reaper, the owner-specific sweep and the guard bookkeeping, at the lowest risk. |
| 2 | Dream runs (`dream_runs`, `dream_rounds`) | It has the least protection today: no transaction, blind writes, two advance paths that can both send a round, and AUT-06's endless re-queue. There are few callers, and nothing user-facing waits on it. |
| 3 | Goals (`task_plans`, `task_items`) and the native-goal gate | It is mostly protected already. What it needs is conformance, AUT-20's gate change and the AUT-N1 follow-ups. |
| 4 | Turns (per-turn instance, launch and slot) | It is the most severe (TP-01/10/11) and also the widest. About 40 call sites in 12 modules write it, from at least three kinds of process (runtime, server, each Claude hook). It is runtime-class, so it can only release at 18:00, and it overlaps S4. It converts inside or right after the S4 turn engine slice, on its seams. |

Severity alone would put turns first. The order instead puts the conversion
that can do the most damage last. By then three conversions will have proven
the kit, and the S4 seams will be in place. The queue conversion also settles
the receipt side of turn settlement (`settle_receipt`, `mark_started`, the
stale-claim reset) before turns depend on it.

## Rules for every conversion

### Commit order and the guards

A module-level `Lifecycle` declaration makes its column owned the moment it
exists. From then on, `test_table_writers.py` accepts only the owner as a
writer, and it marks every `STATUS_WRITERS` entry for that column stale, even
one that names the owner (:277-291). `test_lifecycle_guards.py` finds the
declaration whether or not production code calls it (:108-126). So each
conversion runs in this order:

1. **G0.** Characterization tests of today's transitions, written against the
   old code.
2. **Consolidate.** Move every foreign writer of the column into the owner
   module, as owner functions that others call. Skip this when the owner is
   already the only writer (true for all four candidates at the module
   level; the hook processes and the agent CLI call owner functions).
3. **Activate, atomically, in one passing commit.** Declare the `Lifecycle`,
   remove the column from `UNCONVERTED`, and remove its `STATUS_WRITERS`
   entries. Writers inside the owner module may still be the old code at this
   point, because the guard is per module.
4. **Route.** Send each writer inside the owner through `lk.transition` or
   `lk.compare_and_set`, one writer per commit where the change is not
   trivial.
5. **Effects.** Move them into the outbox, subject to the outbox limits
   below.
6. **Reaper.** Add it, subject to the reaper rules below.

A different staging, for example a declaration that is not yet active, would
be a new guard mechanism. This plan does not need one.

Steps 4 to 6 change behaviour wherever the old code was blind or had no
deadline. Each such change is a registered intended difference (G2), named
before the run. A step that keeps behaviour says so and registers nothing.

### One owner is one module, not one process

SQLite compare-and-set works across processes. A hook subprocess or the agent
CLI may therefore call the owner's functions. What they may not do is write
the column themselves.

### Work outside the writer lock

The kit's transaction is the global SQLite writer lock. Inside it go:
- pure decisions;
- compare-and-set;
- revalidation of a snapshot read earlier: a revision, a status, a lease, or a
  few small fields through `json_extract`.

Outside it, before the transaction, go:
- process and runtime inspection;
- runtime RPC;
- backend checks;
- parsing or serializing large documents.

The decision inside then checks that the snapshot those reads came from is
still current, and gets a `Conflict` if it is not. This is today's pattern in
goal recovery (`task_goal_recovery.py:216-224`, :239-244) and in the queue's
idle sweep (`turn_queue.py:361-407`), and it is kept. No conversion may move
an external read under the writer lock.

### The outbox is not durable

`Outbox` is an in-memory list (`lifecycle.py:174-190`). `transaction` commits
before it runs the outbox (:193-207), and a failed effect does not undo the
commit. So the kit guarantees two things only:
- no effect announces a state that did not commit;
- concurrent writers cannot both win a transition.

It does not guarantee that an effect runs exactly once:
- a crash after `COMMIT` and before the effect loses the effect;
- a crash, or an unclear result, after a dispatch leaves the external world
  ahead of the database.

Every conversion whose effects matter describes four cases:
- a crash before the effect;
- a crash after the effect but before its receipt is recorded;
- a refusal (the agent is busy, say);
- `EffectsFailed`.

For each case it states:
- the durable marker that makes recovery possible, such as a sent-at stamp, a
  delivery column or a client id that the receiving side deduplicates on;
- the recovery path, usually the lifecycle's own deadline;
- the replay or characterization fault case that proves it.

Where an external effect cannot be made idempotent, the plan says "at most
once", or "at least once, deduplicated by X". It never says exactly once.

### Reapers are bounded

Today, `lk.reap` fetches every overdue row and updates each of them in one
call (`lifecycle.py:263-271`). An index bounds how the lookup runs, not how
many rows it updates or how long it holds the writer lock. The reaper must
not repeat the 09:14 lock incident (AUT-N1). So the kit change below, number
4, makes reaping bounded:
- **Batches.** Each batch is at most N rows (default 100), in keyset order
  `(entered_at, key)`, and runs in its own `lk.transaction`.
- **Eligibility hook.** An optional `eligible(row) -> event | None`, run
  inside the batch transaction. Through it the owner can apply its own
  conditions to the snapshot it revalidates, for example a liveness result
  computed before the batch, or a persisted attempt count. It can also fire
  an event other than `deadline_expired`, such as the dream round's
  `abandon` once the cap is reached.
- **Tick budget.** The host stops after a budget of rows and milliseconds per
  tick, and resumes from the keyset cursor on the next tick.

One server tick hosts the database reapers. The candidate is the scheduler
loop that already runs the goal recovery tick. In-memory lifecycles (slots,
the Live phase) are reaped by their owner, under the owner's lock. Every
reaper also goes on the program's hot-path watch.

### Schema, client contract and replay

- **Indexes and `entered_at` columns are migrations.** They are
  contract-owner commits: `db_schema.py`, `db_migrations.py`, the goldens and
  the rehearsal. Each needs the lead's go per table, and none may overlap the
  S1 end window.
- **The client contract is frozen.** State values a client sees (queue
  receipts, goal status, the agent's state) keep their spelling. A new
  client-visible state is a `HOST_CONTRACT` bump with a row in
  `docs/compatibility.md`, which is a lead decision.
- **Replay.** Each conversion needs scenarios that reach its states. These
  exist:
  - `queue-while-busy`, `queue-cancel` and `stop-with-queue`;
  - `goal-turn`, `stop-mid-turn` and `preempt-live-process`;
  - `retry-same-client-id`.

  These are missing and will be asked of the harness owner before the
  conversion starts:
  - a dream round;
  - a goal recovery tick;
  - fault injections at the outbox boundary (a crash after commit, and an
    effect that fails).

## Kit changes this plan needs

The contract owner makes each change, and reviewer A reviews it, before the
conversion that first needs it.

1. **Correction edges** (the queue): `Edge(..., correction=True)`.
   - A correction runs only from a final state to a final state.
   - The declaration lists exactly which pairs are allowed.
   - Ordinary edges still reject final sources.
   - A correction is compare-and-set, idempotent (re-applying the same
     target is a no-op, not a `Conflict`), and its effects are explicit.
2. **A deadline that expires to a non-final state** (queue claims, dream
   retries).
   - The target must be a declared state with its own valid wait.
   - Deadlines stay positive, and the reachability checks still apply.
   - A cycle a deadline drives on its own must be capped by a persisted
     counter, decided in the same transaction (the dream attempt cap).
     Reachability in the graph alone is not a liveness proof.
3. **A log-shaped lifecycle** (turns): compare-and-set on an append-only
   table. It is designed with S4, subject to the turn invariants in section 4.
4. **Bounded reaping**, as described above: batches with a keyset cursor, the
   eligibility hook, and the tick budget.

## 1. Queue rows

**Today** (`turn_queue.py`).
- **States:** queued, claimed, parked and started, then done, failed,
  interrupted, ended, unknown, cancelled and withdrawn.
- **Writes:** almost all are compare-and-set (`WHERE status IN (...)`).
  `BEGIN IMMEDIATE` wraps `move_to_back`, the terminalizers, `remove`,
  `cancel`, `update_text`, `remove_for_agent` and `set_paused`.
- **Writers:** turn_queue.py is the only module that writes the column. Both
  the server and the runtime call it.
- **Effects:** none inside the module.

**Breaks the kit:**
- `settle_receipt` (:280) lets a newer settlement overwrite a settled
  receipt, which is an edge out of a final state. Fix: correction edges,
  limited to the settlement pairs `_SETTLEABLE` allows today.
- `_move_behind` (:86) deletes and reinserts.
- `allow_paused_for` (:621) writes a flag with no guard. It is not the state,
  so this is not a lifecycle problem.

**Waits.** Each one keeps today's behaviour, including its gates.
- **claimed:** `Deadline(30 s, expire_to=queued)`, declared. It is reaped by
  the owner's sweep, which keeps `reset_stale_claims`' `exclude` argument
  (rows that agent_rings already settled, :649-659). The exclusion is applied
  through the eligibility hook.
- **started:**
  `WaitForever("its turn's settlement ends it; the owner's idle sweep repairs a missed settlement")`.
  - It is not a plain age deadline, because `terminalize_idle_agent_receipts`
    (:361-407) is not one. A receipt ends only when the agent is not live (an
    inspection error counts as live) and has no busy state. Then either:
    - a terminal state newer than the receipt is at least 60 s old; or
    - the receipt is older than 24 h.
  - A plain `Deadline(24 h)` would end a turn that is still running, and it
    would lose the 60 s repair.
  - So the sweep stays owner-specific:
    1. Run `is_live` outside the writer lock.
    2. Revalidate the newest state and the receipt inside a bounded batch.
    3. Settle through the kit with event `idle_settle`.
- **queued:**
  `WaitForever("dispatch, cancel or the agent's deletion ends it")`.
- **parked:** `WaitForever("the user resumes or cancels the paused queue")`.

**Commits:**
1. Kit changes 1, 2 and 4.
2. Activate `QUEUE_RECEIPT`: the declaration, with `UNCONVERTED` and
   `STATUS_WRITERS` removed together.
3. Route each writer through the kit.
4. `_move_behind`: either an update of the position, or one documented
   transaction.
5. Move the claim reset and the idle sweep onto the bounded reaper with their
   gates.

**Behaviour changes to register:** none intended. If commit 4 changes the
order of rows with equal positions, that is registered.

## 2. Dream runs

**Today** (`dreaming.py`).
- **States:**
  - `dream_runs.status`: active, then completed, abandoned or noop (all
    final). `stage` is free text.
  - `dream_rounds.status`: queued, sent, then completed or abandoned. A sent
    round can go back to queued.
- **Transactions:** none in the module.
- **Writes:** blind, except `_recover_stale_sent_round` (:1082) and the round
  update in `abandon_run` (:871).
- **Processes:** two. `process_assistant_text` arrives from the runtime
  (`turn_dispatch.py:3565`), and the scheduler runs in the server
  (`server.py:7142`).

**Breaks the kit:**
- `_send_dream` dispatches the turn (:2203) before `mark_round_sent` (:2209).
  The scheduler's `run_once` and the `request_advance` / `_advance_when_ready`
  threads (:2237) both reach `_advance_run`, so two paths can send the same
  round.
- A sent round goes back to queued after 90 minutes with no attempt cap
  (AUT-06).
- An active run has no deadline.

**Declarations:**
- **`DREAM_ROUND`:**
  - queued: `Deadline(24 h, abandoned)`.
  - sent: `Deadline(90 min, expire_to=queued)`, using kit change 2.
  - `attempts` is persisted and checked by the reaper's eligibility hook in
    the same transaction. At the cap (proposed: 3) the hook fires `abandon`
    instead of the expiry.
- **`DREAM_RUN`:** active: `Deadline(48 h, abandoned)`.

**Dispatch.** The round is claimed queued→sent by compare-and-set, with
`sent_at` and `attempts + 1`. The dispatch runs from the outbox and carries
the round's id as its client id, so the queue's dedup on retries
(`retry-same-client-id`) makes a re-send of the same round attempt a no-op.

What this guarantees: **one winner per claim**, and **at least once,
deduplicated per attempt**, for the dispatch. It does not guarantee that a
round runs exactly once. The four cases:
- **Crash after the claim commits, before dispatch:** the round sits in sent
  with no turn. The sent deadline returns it to queued, and that counts an
  attempt.
- **Crash, or an unclear result, after dispatch and before the round's
  output is recorded:** a provider turn may be running.
  - Before the deadline re-queues the round, the eligibility hook needs a
    liveness result for the dreamer agent. That result is computed outside
    the lock, and the hook revalidates it inside.
  - While the agent is live with this round's trace, the round stays sent.
  - A late output for an attempt that has been superseded is recorded only
    if the round is still in that attempt; otherwise it gets a `Conflict`.
- **Busy refusal:** the claim is reverted sent→queued with event `refused`,
  without counting an attempt, so a busy agent never uses up the cap.
- **`EffectsFailed`:** logged. The round stays sent, and its deadline
  applies.

**Digest delivery** (`_deliver_digest`, `harvest_worktree`) gets a durable
`delivered_at`. The final transition commits first. Delivery then runs,
idempotent per run id, and stamps `delivered_at`. A bounded reaper batch
retries undelivered completed runs.

**Commits:**
1. Activate the declarations, with `UNCONVERTED` and `STATUS_WRITERS` removed
   together.
2. The claim, then dispatch from the outbox with the client id.
3. Route the remaining writers through the kit.
4. Delivery with `delivered_at`.
5. The bounded reaper with the attempt cap and the run deadline.

**Fault cases for replay:**
- a crash after the claim;
- a crash after dispatch;
- a busy refusal;
- a delivery failure.

**Behaviour changes to register:**
- A round that fails three times is abandoned (fixes AUT-06).
- A run active for more than 48 h is abandoned.
- Concurrent advance paths no longer both dispatch one attempt.

These need the lead's classification as fixes inside a conversion.

## 3. Goals

**Today:**
- **`task_plans.status`:** active, paused and blocked; final: completed,
  cancelled and superseded.
- **`task_items.status`:** pending, in_progress and blocked; final:
  completed, skipped, deferred and removed.
- **`goal_json.continuation.state`:** the recovery state machine, inside the
  document.
- **`agent_goals`:** the provider's mirror of a native Codex goal
  (`agent_goals.py:3-6`), written from `codex_app_server.py:402`.

**What is a defect and what is conformance.**
- **`cancel_for_agent`** (:304-323) is the one current concurrency defect.
  It writes the status blind and in autocommit, and is called from
  `agents.soft_delete`. It becomes a compare-and-set inside a transaction.
- **`_goal_mutate`** (:589-614, status write at :708) already takes
  `BEGIN IMMEDIATE`. It reads the row, checks the revision and rejects closed
  statuses inside that transaction. Its status `UPDATE` lacks an SQL state
  predicate, but it is not an unlocked race. Routing it through the kit is
  conformance, not a fix.
- **`_goal_save`** (:440-451) writes only `goal_json`, never the status. So
  does its facade alias `task_goal_state._save`, which recovery's fallback
  uses (`task_goal_recovery.py:432`). Neither is a status writer.
- **Recovery** already follows the rule on work outside the writer lock:
  - `_claim` keeps `boundary()`'s process, runtime and backend inspection
    outside SQLite (:239-244, :87-96, `reconcile.py:50-74`);
  - `_commit` serializes the JSON outside the transaction and revalidates
    text, revision, status and enrolment inside its short transaction
    (:216-224, `task_plans.py:455-472`).

  The conversion keeps both. It moves no boundary read and no JSON work under
  the writer lock. Inside, it adds only a pure decision and compare-and-set
  on the revalidated snapshot.

**AUT-N1 follow-ups.** These are separate fix-lane commits, independent of
the boundary rule above:
- **A byte bound on `goal_json`.**
  - History is capped at 200 entries (`GOAL_HISTORY_LIMIT`), but a replan
    appends `prior_steps` with up to 500 full item rows (:840).
  - The legacy `history_json` (:529-535) has no cap.
  - Fix: bound the document in bytes and move `prior_steps` to the
    append-only `goal_ledger`, so every rewrite stays small.
- **Recovery's continuation state into a column** (a migration). It can then
  be compared and set and reaped without rewriting the document.

**AUT-20 is a gate change, not a deadline.** `boundary()` blocks Host
recovery whenever a mirrored goal has `native` set and a status other than
`complete` (`task_goal_recovery.py:75-85`). A deadline that moves the mirror
from active to blocked still meets that condition. It would also invent
ownership from silence: the mirror belongs to the provider, and a stale
mirror does not prove the provider has let go. The plan is:
- `agent_goals` stays the provider's mirror, not a Host lifecycle. It moves
  to `NOT_LIFECYCLES`, with that reason, once the gate below exists.
- The gate blocks while the native goal is fresh. A goal is fresh while its
  mirror was updated within the heartbeat window, or the provider reports it
  running.
- When the mirror is stale, recovery asks the provider before starting
  competing work. For Codex that is the app server's state for the thread
  and goal; whether it exposes enough is to be confirmed in the G0 commit. A
  provider with no such query always takes the no-answer branch below. The
  query runs outside the writer lock, and the claim revalidates the mirror's
  revision inside.
  - Provider says the goal is complete or gone: the gate opens, and the
    mirror is updated from that answer.
  - Provider says it is running: the gate stays closed, and the mirror is
    refreshed.
  - No answer (the provider is unreachable, or the thread is unknown):
    recovery moves to `attention` for the user. It does not take over.
- The heartbeat window is a lead decision. The gate semantics are not.
- Proof: a characterization test of a stale native row that blocks today, and
  replay or unit cases for each of the three answers.

**Waits:**
- **Plan, active:** `WaitForever("the agent works it; recovery's own deadlines bound each attempt")`.
- **Plan, paused:** `WaitForever("the user resumes it")`.
- **Plan, blocked:** `WaitForever("a person unblocks it")`.
- **Recovery:**
  - dispatching, admitted and queued: the 60 s lease, as today;
  - retry: `Deadline(backoff)`;
  - attention: `WaitForever("the user")`.

**Commits:**
1. The byte bound and `prior_steps` to the ledger, through the fix lane.
2. The continuation column.
3. Activate the plan and recovery declarations, with `UNCONVERTED` and
   `STATUS_WRITERS` removed together.
4. `cancel_for_agent` as a compare-and-set inside a transaction.
5. Route `_goal_mutate` and the rest through the kit, which is conformance.
6. The native-goal gate.
7. The bounded reaper for recovery.

**Behaviour changes to register:**
- A cancel racing a finish is a `Conflict`, not an overwrite.
- A stale native goal leads to a provider check and then either recovery or
  `attention` (AUT-20).
- Larger plans keep a shorter inline history.

## 4. Turns

**Today.**
- **`state_log.kind`:** the agent's visible state (`AgentState`), as an
  append-only log.
  - spawned is written by `AGENT_CREATED` (`agents.py:191-204`,
    `turn_lifecycle.py:122`): the agent was created, not a process launched.
  - `SPAWN_STARTED` writes thinking (:125).
  - The terminal states are done, idle, interrupted and stopped.
- **`turns`:** `ended_at`, `settled_at` and `outcome`.
- **Slots:** in memory under `OwnershipLock`.
- **Writers:**
  - The default writer, `transition()` (:301), reads and inserts in separate
    autocommit statements, and `force=True` skips the check.
  - `_conditional_transition()` (:360-405) is the one guarded path. Inside
    `BEGIN IMMEDIATE` it fences on three things: the newest `state_id`, a
    live trace, and an open unsettled turn and its prompt admission.
- **Settlement:** `_settle_turn` (:192) settles in three autocommit
  statements.
- **Slot cleanup:** `_orphaned_spawn` (`turn_dispatch.py:3440-3450`) is a
  check on the age of a slot claim, guarded by "no launch or account
  recovery". It is not a deadline on any state.

**The instance is the turn, not the agent.** Agents run turns again and
again. A kit instance cannot leave its final set, so the agent's visible
state cannot be the lifecycle instance. The conversion declares:
- **`TURN`:** one instance per turn, keyed by the turn row and its trace.
  Its states run from admitted, through launching and running, to settled.
  Final states carry the outcome.
- **`LAUNCH`:** the CLI process, owned by the turn engine and the process
  registry: launching, then alive, then exited.
  - launching: a launch lease, `Deadline(launch grace, failed)`.
  - alive: `WaitForever("the process is alive")`, backed by missed-exit
    reconciliation. A process the registry cannot find past a grace is moved
    to exited with an unknown status, through compare-and-set.
- **The slot:** an in-memory lifecycle owned by `turn_slots`, tied to the
  turn instance that holds it.

The agent's visible state (`state_log`) becomes a projection, written by the
`TURN` owner in the same transaction as the turn's transition. It is no
longer a lifecycle of its own. A newly created agent (spawned) is not a turn
and gets no deadline.

**Invariants the log adapter (kit change 3) must keep.** A generic check
that the newest row is unchanged is not enough. A projection write is
conditional on the `TURN` instance's state, and keeps today's fences:
- the live trace;
- the open unsettled turn;
- the prompt admission.

The adapter tells apart:
- the causal generation (`state_id`);
- the visible head, ordered by timestamp;
- the historical receipts, which stay.

Hook writes become events on the `TURN` instance. A late hook gets a
`Conflict` against a settled turn and cannot move the agent's state.

**Settlement** is one transaction: the outcome, the restamp, the projection
and the queue's settle edge (from conversion 1).

**Spawn.** Commit `TURN` admitted→launching and `LAUNCH` created, then start
the process from the outbox. There are three crash cases, covered by the
launch lease and missed-exit reconciliation:
- **A crash before the process starts:** launching expires to failed, and
  the turn settles as failed.
- **A crash after the process starts, before alive is recorded:** the
  registry finds the process and moves it to alive, or the lease expires.
- **Preemption (TP-01):** the new spawn's effect waits for the old `LAUNCH`
  to reach exited.

**TP-N1c's structural backstop is here.** TP-N1c is the lead-accepted
residual: when `on_error` terminal persistence is exhausted, a slot can stay
held. In Phase 3 the slot is tied to its `TURN` instance, and that instance
is tied to a `LAUNCH` with a lease and missed-exit reconciliation. The
owner's reaper frees a slot when its turn's process has exited and the turn
is still unsettled past the grace. It also settles the turn (`interrupted`)
through compare-and-set, which retries the persistence that was exhausted.
TP-N1c stays accepted until this lands. It is not reopened in the fix lane.

**Waits.** The busy states are backed by `LAUNCH`, not by deadlines on
`AgentState`:
- **TURN:**
  - admitted: `Deadline(admission grace, interrupted)`;
  - launching: the launch lease;
  - running: `WaitForever("its process is alive; LAUNCH's exit settles it")`.
- **waiting** (a prompt to a person) is a projection of running:
  `WaitForever("a person answers")` at the turn level.
- **background:** the projection of a finished turn with a background job.
  The background job's own lifecycle (AUT-03) bounds it, not the turn.

**Behaviour changes to register:**
- A late hook cannot overwrite a settled turn.
- `turns.ended_at` is set on normal completion (TP-11).
- The next process starts only after the old one exits (TP-01). The
  `preempt-live-process` scenario already reaches this.
- A held slot of an exited, unsettled turn is freed (TP-N1c).

## Open questions for the lead

1. The order: queue, dream, goals, turns, with turns tied to S4.
2. Phase 3 needs migrations: `entered_at` columns, reaper indexes, the goal
   continuation column, `dream_rounds.attempts` and `delivered_at`. May the
   contract owner make them, one per conversion, after the S1 end window?
3. Values for these behaviour numbers:
   - the dream attempt cap (3);
   - the run deadline (48 h);
   - the native-goal heartbeat window;
   - the reaper's batch and tick budget.
4. The goal follow-up, the byte bound with `prior_steps` moved to the
   ledger. It can go through the fix lane now. Should it?
