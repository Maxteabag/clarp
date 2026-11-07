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

### Design delta (5 Nov 2026), from the G0 matrix

The G0 matrix (`tests/unit/test_queue_receipt_matrix.py`, on main at `28973509`)
gives today's exact transitions. Writing the declaration against it changes
four things in the plan above. No change touches behaviour, the schema or a
stored value.

1. **Per-state entry times (kit change 5).** `queued_turns` has no single
   "entered at" column, so the kit's one `entered_column` does not fit.
   - Today only two states record their entry time: claiming sets
     `claimed_at`, and starting sets `started_at`. Releasing a claim clears
     `claimed_at`, and no other transition stamps a time.
   - The kit therefore gains `entered_columns`, a mapping such as
     `{"claimed": "claimed_at", "started": "started_at"}`. It is used
     instead of `entered_column`, never together with it.
   - A transition stamps `now` only into its target state's column, if that
     state has one. Only that column is reserved: a caller's values may not
     overwrite it, but may clear another state's column. Three writers clear
     `claimed_at` today, and keep doing so as an explicit value:
     `release_claim` (claimed → queued), `cancel` (claimed → cancelled) and
     `reset_stale_claims` (claimed → queued).
   - The reaper reads the source state's column. Validation requires a
     column for every state with a `Deadline`.
   - A NULL entry time counts as 0, so the row is overdue. This holds in the
     reaper's select, its order and its keyset. Today's claim reset does the
     same with `COALESCE(claimed_at, 0) <= cutoff`, and `claimed_at` is
     nullable. A G0 case pins it: a claimed row with a NULL `claimed_at` is
     reset by the sweep.
   - The alternative was a migration adding a `status_at` column. That would
     write a new stored value on every transition, which raises the
     reader-first and rollback questions. The mapping avoids both.
2. **Settlement events, ordinary and correcting.**
   - `settle_receipt` moves a `started` receipt to done, failed,
     interrupted or ended (an ordinary edge). It also moves one settled
     outcome to another (the newest settlement wins), which is a correction.
     Today one SQL statement does both.
   - Since kit review A2 (`228a4454`), an event cannot be both. So each
     outcome gets two events: `settle_<outcome>` from `started`, and
     `resettle_<outcome>` from the three other settled outcomes.
   - The correction pairs are exactly those the matrix shows. A `cancelled`,
     `withdrawn` or `unknown` receipt is never resettled.
3. **Every status writer goes through one set-based compare-and-set (kit
   change 6).** Each writer today is a single
   `UPDATE ... WHERE <key or filter> AND status IN (...)`. It returns the
   rowcount (or a bool made from it), and some add predicates of their own:
   `withdraw_unclaimed` adds `claimed_at IS NULL`. `lk.transition` would not
   keep that, for three reasons:
   - it reads, decides and then writes, so one statement becomes two;
   - it raises `IllegalTransition` or `Conflict` where today's writers return
     False, 0 or None (the matrix records those values);
   - it cannot carry an extra predicate.

   So no queue writer uses `lk.transition`. Each one, single-row writers
   included, calls
   `compare_and_set_where(con, lc, events, where, params, values=)` with its
   own filter (`queue_id = ?` and any extra predicate). That call runs:

   ```sql
   UPDATE t SET state = <target>, ... WHERE state IN (<declared sources>) AND <where>
   ```

   - The sources come from the declaration, never from the caller.
   - It runs one statement per target state and returns the count, or the
     rows changed if `RETURNING` is asked for. Each writer maps that to its
     return value exactly as today.
   - **Transaction rule.** If the connection is already in a transaction, the
     call joins it. That is the case in agent_rings' transactions
     (`claim_for_dispatch`, `withdraw_claimed`, `release_claim` and
     `mark_unknown` at `agent_rings.py:1875-1913`, `:1958` and `:2030-2044`)
     and in `cancel_for_traces`' caller. If not, an event set with one target
     runs as one autocommit statement, as today
     (`turn_dispatch.py:736, 1417, 2033-2076, 2410, 2724`). An event set with
     several targets wraps its statements in one `BEGIN IMMEDIATE`.
   - The two terminalizing sweeps (`turn_queue.py:337-341`, `:413-417`) loop
     one `UPDATE` per row today. They keep that loop, with one call per row,
     so their statement count is unchanged.
4. **What stays outside the kit, named in the declaration's comment.**
   - **Inserts** (`enqueue`, `park`, `park_at_head`) create a row directly in
     `queued` or `parked`. An insert cannot race a status, and the kit's
     `create` knows one initial state. Declaring `parked` as a second entry
     state would be a seventh kit change, for no safety gain.
   - **Deletes** (`remove`, `remove_taken_over`, `unpark`, `discard_parked`,
     `remove_for_agent`) take a row out of the lifecycle with their own
     status predicates, as today.
   - **`allow_paused` and `allow_paused_for`** write a flag, not the state.

**Gaps in G0.** Two behaviours are not pinned yet:
- `mark_launched_call_open_unknown`'s effect, because it acts only on
  `call-open:` ids;
- the claim reset of a row with a NULL `claimed_at`.

A tests-only commit on main covers both before any conversion commit.

**Declaration** (`QUEUE_RECEIPT`, in `turn_queue.py`). Event names follow the
functions:

- **Initial and final states.** The initial state is `queued`. The final
  states are done, failed, interrupted, ended, unknown, cancelled and
  withdrawn.
- **Edges:**
  - `claim`: queued → claimed (also `claim_for_dispatch`);
  - `release`: claimed → queued;
  - `start`: queued or claimed → started;
  - `park`: queued → parked (`take_over_at_head`);
  - `requeue`: parked → queued;
  - `cancel`: queued, claimed or parked → cancelled (`cancel_for_traces`
    leaves out parked);
  - `withdraw`: queued or parked → withdrawn;
  - `withdraw_claimed`: claimed → withdrawn;
  - `mark_unknown`: queued, claimed or parked → unknown;
  - `call_open_unknown`: claimed or started → unknown;
  - `settle_<o>`: started → done, failed, interrupted or ended;
  - `resettle_<o>`: corrections among done, failed, interrupted and ended,
    matching the matrix;
  - `idle_settle` (the idle sweep, `:415`): started → ended only;
  - `ended_settle` (the ended sweep, `:325-341`): started → done, failed,
    interrupted or ended. Both are ordinary edges that share targets with
    `settle_<o>`.
- **Waits:**
  - claimed: `Deadline(30 s, expire_to=queued)`;
  - started, queued and parked: `WaitForever`, as in the waits above.

**Commits, revised** (on refactor/integration):
1. Kit changes 5 and 6, on main (they have no production caller).
2. The G0 top-up for `call-open:` rows, on main.
3. Activate `QUEUE_RECEIPT`.
4. Route the single-row writers through `compare_and_set_where`.
5. Route the set-based writers and the per-row sweeps through it.
6. `_move_behind`.
7. The bounded reaper with the claim-reset `exclude` and the idle sweep's
   gates.

**S4 boundary** (agreed with rf-s4, 5 Nov): none in `turn_dispatch.py`. The
conversion edits only `turn_queue.py`, and every public function keeps its
name, signature and return values.

Each commit keeps the G0 matrix byte-identical, and G2 between main and
integration stays at zero.

### Queue reorder transaction (Phase 3, commit 6)

Keep the existing delete/reinsert operation, shared by `_move_behind` and
`move_to_back` as `_reinsert_at_back`. It changes only `queue_seq`: every
other column, including status, entry times, admission and pause rights, is
copied from the same row. This is a reorder within the receipt's lifecycle,
not a status transition. Allocating the sequence through the existing
AUTOINCREMENT insert also preserves SQLite's high-water mark; computing
`MAX(queue_seq) + 1` would reuse positions after deletions.

The transaction has one owner:

- `move_to_back` owns its existing `BEGIN IMMEDIATE` through commit or
  rollback, including the revision bump.
- `park_at_head` and `take_over_at_head` join their caller's existing write
  transaction. The dispatcher owns that transaction in `_park_at_head` and
  `TurnDispatchService.transfer_recovery_parks`. It covers the new or parked head,
  every other open row reinserted in their original sequence order, and the
  revision bump. For a recovery transfer it also covers turn settlement and
  the handoff projection.
- Neither `_move_behind` nor `_reinsert_at_back` begins or commits a
  transaction. A nested lifecycle transaction would refuse these callers;
  an inner commit would split their atomic handoff.

There is no new stored value, status, transaction boundary or intended
behaviour difference. The unchanged G0 matrix pins `move_to_back`,
`park_at_head` and `take_over_at_head`, including ordering, copied row fields
and revisions. The two dispatcher entry points keep their names and code.

### Queue reaper (Phase 3, commit 7)

The queue owner uses `lk.reap_tick` for claim reset and the idle repair. Each
owner call now examines at most 100 candidate receipts, with a keyset cursor
kept per thread and database connection; skipped rows count, and cursor wrap
makes them eligible for another observation. This conservative page size
leaves room for the snapshot and revision SQL within the lead's maximum of
500 rows and 200 ms per tick. Every receipt batch and revision batch is at
most 100 writes and is measured against the 50 ms budget. The first
production-shaped synthetic probe at `61e533f5` met the batch limit but
reached 320 ms for an owner tick; it must not be treated as passing evidence.

- **Claims.** Excluded opening rows are filtered in SQL before the limit,
  so a prefix of excluded rows cannot hide an eligible claim. The reset
  keeps its own clock, separate from the opening owner's earlier cutoff;
  zero and negative ages still mean a zero-age cutoff. A NULL claim time
  still means 0. Expiry clears `claimed_at` through the event-specific CAS
  values. Revisions are bumped once per changed agent in each invocation,
  after its receipt transaction commits, with at most 100 revision writes
  grouped in another transaction. A failed receipt batch announces nothing.
  An explicit outer transaction is joined by one bounded `lk.reap` call,
  with revisions in that same transaction; it is never committed here.
- **Idle repair.** `started` remains `WaitForever`, not a plain age
  deadline. The owner snapshots a bounded page and performs `is_live` outside
  the writer lock; an inspection error still counts as live. Inside the
  batch it reads the latest persisted state through the existing
  `idx_state_log_agent_ts` index and applies the busy, newer-terminal/60 s
  and 24 h gates to the current row. A NULL `started_at` still uses
  `enqueued_at` for age; the NULL=0 keyset is only paging order.
- **Signed difference.** Lead decision 546, "Queue reaper race fence" in
  `differences.md`: a newer busy or newly ineligible persisted state arriving
  between snapshot and CAS keeps the receipt open. An unchanged state gives
  the prior result. This replaces the old snapshot race; it is intentional.
- **Caller ownership.** `TurnDispatchService.recover_queued` serializes
  recovery and calls `_recover_queued_locked` without a SQLite transaction;
  `resolve_stale_claims` exits its opening transactions before the generic
  reset. Idle repair is called from runtime startup and the background
  watcher's reconcile pass, neither of which owns a transaction.

The kit's optional `states`, absolute `cutoffs`, `values_by_event` and SQL
`where`/bound `params` support these policies without new stored states,
timestamps or indexes. A `WaitForever` selection requires an eligibility
hook that chooses a declared event; it cannot expire by age. A tick reserves
the next batch's time budget before starting it and counts commit/outbox
time. The opt-in synthetic benchmark is
`CLARP_QUEUE_REAPER_BENCHMARK=1` with
`tests/unit/test_queue_reaper.py::test_production_size_reaper_budgets`, on
heavy-remote only; it populates one million synthetic state-log rows and
1,000 receipts and checks both real batch and whole-owner tick timings.

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
- **`DREAM_RUN`:** active: `Deadline(12 h, abandoned)`, as AUT-06 is signed
  today; the conversion must not loosen it.

**Dispatch.** Today a dream round does not go through the chat queue.
`dispatch_adapters.dream` (:98-107) calls `dreaming.dispatch_isolated_dream`
(:1140-1198), which starts `backends.spawn_turn` directly with
`isolated=True`, in a fresh backend session with no live-chat binding. The
queue's client-id dedup is therefore not on this path, and the conversion
does not route dreams into the agent's chat session to borrow it. The
isolation stays. The dream owner becomes its own durable receiver:

- **Attempt identity.** The claim (queued→sent, compare-and-set) allocates
  the attempt in the same transaction. It sets `attempts + 1` and
  `attempt_id = <round id>:<attempts>`, and stores that attempt's
  `trace_id` and `backend_session_id`. A new attempt always gets a new id. A
  redelivery of the same attempt always reuses the stored one.
- **The launch fence and its owner.**
  - **Who launches.** Dreams are dispatched only from the server (the
    scheduler's `run_once` and the `request_advance` threads). The owner is
    that server process, identified by its boot id and pid.
  - **Order of operations.** The owner's launch lock is taken before SQLite,
    the project's usual owner-lock-before-SQLite order. Under that lock the
    owner:
    1. registers the attempt as tentatively in flight;
    2. runs the compare-and-set that stamps `launched_at` and `launch_owner`,
       where the round is sent, has this `attempt_id`, and
       `launched_at IS NULL`;
    3. drops the tentative registration if the compare-and-set loses.

    Recovery in the same process reads launch rows only under the same lock.
    So it can never see a committed `launched_at` from this boot without the
    registration.
  - **Releasing the registration.** It is released only after one of two
    durable outcomes below, `handed_off` or `no_execution`. It is never
    released by blind `finally` or exception cleanup. A paused winner
    therefore cannot launch after a replacement. Each attempt runs **at most
    once**. The output fence below is a second line of defence, not the
    guard against overlap.
- **Launch outcome, recorded durably on the attempt.** It is one of three
  values:
  - **`no_execution`:** the backend refused before handing off, meaning
    before any process creation or app-server request. This needs a typed
    pre-handoff refusal from the backend: `spawn_turn` raises it only before
    `Popen` or the request. Proven nothing ran, so the round may be
    re-queued.
  - **`handed_off`:** `spawn_turn` returned a handle and the owner persisted
    the execution identity. If persisting the identity fails (for example
    the database is locked), the owner keeps the returned handle in memory
    and retries. The registration stays until the write succeeds.
  - **`uncertain`:** any other exception. Today a process can already exist
    at that point: `stream_json.py:80-87` runs `Popen` before `make_handle`,
    and Codex's `start_turn` launches at `backend/codex.py:322`, then does
    more work (:323-335). An uncertain attempt is held, surfaces
    `attention`, and is never replaced until authoritative execution
    evidence turns up. Absence in the local registry does not count as that
    evidence. If even the `uncertain` write fails, the in-flight
    registration stays for the life of the process, which also holds the
    round.
- **Execution evidence per attempt.**
  - **Identity, per backend** (as for `EXECUTION` in section 4):
    - **exec backends:** PID and start token, plus the returned handle while
      the owner process lives;
    - **shared app-server turns:** the thread and turn ids.
  - **No registry lookup.** Isolated attempts are deliberately not
    registered. Codex with `isolated=True` runs `codex exec`
    (`codex_app_server.py:816-824`) with an empty `runtime_agent_id`
    (`backend/codex.py:319`). Isolated Claude does the same (:524), and
    `register_handle` skips empty ids (`backend/base.py:273-276`). Neither
    `active_handles` nor `active_exec_handles` can find them, so neither is
    evidence. Only the returned handle and the persisted identity are.
  - **The outcome is not the end.** `on_result` and `on_error` record the
    attempt's outcome (`result_at` or `error`), not its end. Claude calls
    `on_error` on a rate-limit event while the process is still running
    (`backend/claude.py:615-623`, with `proc.wait` only at :705). It does
    the same on a rejected session binding (:631-639).
  - **The end is a normalized signal**, recorded as `ended_at`:
    - **exec backends:** the returned handle's process exit, after
      `proc.wait`; across boots, a check of the PID and start token showing
      the process is gone or has been replaced;
    - **shared app-server turns:** turn/completed or turn/aborted for that
      turn. This is separate from the transport's own exit.

    A missing registry entry is never an end. This is `EXECUTION`'s
    fail-closed rule, and it applies to dreams from conversion 2 on, not
    only to turns after S4.
- **Output fence.** `on_result` carries its `attempt_id`, and the output is
  recorded only while the round is still sent with that same `attempt_id`.
  Output from an earlier attempt gets a `Conflict` and is logged.
- **Receipt history.** Each attempt keeps its row of evidence (attempt id,
  trace, launched_at and outcome), kept as a tombstone when the attempt is
  superseded. The ordinary queue's receipts are not involved.

What this guarantees: **one winner per claim**, **at most one execution per
attempt**, and a distinct identity for every new attempt. It does not
guarantee that a round runs exactly once. The cases:
- **Crash after the claim, before `launched_at`:** nothing ran. The owner's
  recovery re-runs the dispatch, with the same `attempt_id`, for a sent round
  that has no `launched_at` past a short grace. The fence makes that
  redelivery safe.
- **`launched_at` set, no launch outcome yet:**
  - Owner is this boot: the attempt is in flight, because the registration
    exists until an outcome is durable. Recovery waits.
  - Owner is another boot: **unknown**. The round is held and surfaces
    `attention` after its deadline. It is never replaced automatically.
- **Outcome `no_execution`:** the round is re-queued as a new attempt.
- **Outcome `uncertain`:** the round is held and surfaces `attention` until
  authoritative evidence exists.
- **Outcome `handed_off`:** the attempt may be running, and it is never
  redelivered. The eligibility hook for the sent deadline uses the end
  signal, checked outside the lock and revalidated inside:
  - `ended_at` set: the round is re-queued, and the next claim allocates a
    new `attempt_id`.
  - Still running, even with an error recorded: the round is held.
  - Unknown: the round is held and surfaces `attention`. It is never
    replaced.
- **A failed attempt followed by a new one:** attempt 1 records its error.
  When its real end is recorded, the round is re-queued, and attempt 2 runs
  under its own id. Nothing of
  attempt 1 deduplicates it away.
- **Busy refusal:** the dispatcher declines before the fence (the agent is
  not idle). The claim is reverted sent→queued with event `refused`, without
  counting an attempt.
- **`EffectsFailed`** before the fence is the first case above. After the
  fence it is one of the later cases.

**G0 must prove the actual receiving boundary on the isolated path.** The
`retry-same-client-id` evidence from the ordinary send path does not count
here.

**Digest delivery** (`_deliver_digest`, `harvest_worktree`) gets a durable
`delivered_at`. The final transition commits first. Delivery then runs,
idempotent per run id, and stamps `delivered_at`. A bounded reaper batch
retries undelivered completed runs.

**Commits:**
1. Activate the declarations, with `UNCONVERTED` and `STATUS_WRITERS` removed
   together.
2. Attempt identity, the launch fence with its owner and in-flight set, the
   execution evidence and the output fence. This needs a migration:
   `attempt_id`, `launched_at`, `launch_owner`, `launch_outcome`, the
   execution identity, `ended_at` and per-attempt evidence. It also needs a
   typed pre-handoff refusal from the backends.
3. Route the remaining writers through the kit.
4. Delivery with `delivered_at`.
5. The bounded reaper with the attempt cap and the run deadline.

**Fault cases for replay and characterization**, on the isolated path:
- a crash after the claim, before the fence: the same attempt is redelivered
  and runs once;
- a crash after the fence: no redelivery, then a new attempt id after a
  confirmed end;
- a failed attempt followed by a new attempt that really runs;
- late output from a superseded attempt is rejected;
- an isolated exec attempt that is alive and absent from every registry:
  the round is held, using the returned handle or the persisted PID and
  start token;
- a pause between registration and the launch compare-and-set: recovery,
  under the owner lock, never sees `launched_at` without the registration;
- a spawn that creates the child and then raises before a handle returns:
  the outcome is `uncertain`, the round is held, and there is no
  replacement;
- a successful spawn followed by a locked identity write: the registration
  stays, the write is retried, and there is no replacement;
- a non-terminal error (a rate-limit `on_error`) while a fake exec process
  stays alive: the round is held through recovery, and it is replaced only
  after the real exit;
- a paused launch winner resumes after recovery has run: there is no
  replacement, and exactly one execution;
- a launch owner from another boot with no outcome recorded: the round
  surfaces `attention` and is not replaced;
- a busy refusal;
- a delivery failure.

**Behaviour changes to register:**
- A round that fails three times is abandoned (fixes AUT-06).
- A run active for more than 12 h is abandoned.
- Concurrent advance paths no longer both dispatch one attempt, and an
  attempt is never launched twice.

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
- **`EXECUTION`:** one provider execution of the turn, not an OS process.
  Its states are launching, running and ended, owned by the turn engine. Its
  identity and its evidence of ending are specific to the backend:
  - **Exec backends** (a CLI process per turn): the identity is PID plus start
    token, and the end evidence is that process's exit.
  - **Shared app-server backends** (Codex). `CodexBackend.spawn_turn`
    (`backend/codex.py:187-197`) uses one app-server process for the whole
    Host (`codex_app_server.py:782-806`).
    - The identity is the logical thread and turn, held by its
      `AppTurnHandle` (:73-94).
    - The end evidence is turn/completed or turn/aborted for that turn
      (:360-386).
    - The transport's lifetime is separate: it is the Codex client
      lifecycle. A turn ending never means killing or waiting for the shared
      process, which serves other agents.
  - **Native goal turns** map each provider continuation to an `EXECUTION`
    the same way.

  The waits:
  - launching: a launch lease, `Deadline(launch grace, ...)`. It expires
    only to a state that frees nothing unless an observation confirms there
    is no execution (see unknown below).
  - running: `WaitForever("the execution is running; its end evidence settles it")`.

  **Missed-end reconciliation fails closed.** An observation that is missing
  or failed is not proof of an end. This matches `_agent_has_live_turn`
  today, which counts an inspection error as live
  (`turn_dispatch.py:2228-2234`). There are three cases:
  - **Confirmed end, unknown outcome:** the turn settles as `interrupted`.
  - **Existence uncertain:** the execution stays running, and the slot stays
    held. After a deadline it is surfaced to the user as `attention`. It is
    never treated as free.
  - **Preemption:** it never overlaps an execution whose existence is
    uncertain.

  **G0 cases:**
  - two agents sharing the app-server;
  - one logical turn ending while the transport and the other agent's turn
    stay alive;
  - a lost or unknown observation, which must keep the slot.
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

**Spawn.** Commit `TURN` admitted→launching and `EXECUTION` created, then
start the execution from the outbox. There are three cases, covered by the
launch lease and missed-end reconciliation:
- **A crash before the execution starts:** no PID or handle was ever
  recorded, and the backend confirms that nothing is running under the
  turn's trace. Launching then expires to failed, and the turn settles as
  failed.
- **A crash after the execution starts, before running is recorded:** the
  backend finds the execution by its identity (PID and start token, or the
  thread and turn) and it moves to running. If the backend cannot tell, the
  execution stays held.
- **Preemption (TP-01):** the new spawn's effect waits for the old
  `EXECUTION`'s own end evidence, for example the process exit or
  turn/aborted after an interrupt. It does not wait for a shared transport.

**TP-N1c's structural backstop is here.** TP-N1c is the lead-accepted
residual: when `on_error` terminal persistence is exhausted, a slot can stay
held. In Phase 3 the slot is tied to its `TURN` instance, and that instance
is tied to an `EXECUTION` with a lease and fail-closed missed-end
reconciliation. The owner's reaper frees a slot when its turn's execution has
a confirmed end and the turn is still unsettled past the grace. It never
frees a slot on an observation that is merely missing. It also settles the turn (`interrupted`)
through compare-and-set, which retries the persistence that was exhausted.
TP-N1c stays accepted until this lands. It is not reopened in the fix lane.

**Waits.** The busy states are backed by `EXECUTION`, not by deadlines on
`AgentState`:
- **TURN:**
  - admitted: `Deadline(admission grace, interrupted)`;
  - launching: the launch lease;
  - running: `WaitForever("its execution runs; EXECUTION's end evidence settles it")`.
- **waiting** (a prompt to a person) is a projection of running:
  `WaitForever("a person answers")` at the turn level.
- **background:** the projection of a finished turn with a background job.
  The background job's own lifecycle (AUT-03) bounds it, not the turn.

**Behaviour changes to register:**
- A late hook cannot overwrite a settled turn.
- `turns.ended_at` is set on normal completion (TP-11).
- The next execution starts only after the old execution's own end
  evidence (TP-01). The
  `preempt-live-process` scenario already reaches this.
- A held slot of a turn whose execution confirmably ended, but which is
  still unsettled, is freed (TP-N1c).

## Decisions (lead, 3 Nov 2026)

1. **Order:** approved. Queue rows, then dream runs, then goals, then turns
   (turns together with S4).
2. **Migrations:** the contract owner makes them, one per conversion, on
   refactor/integration when the conversion needs it. They do not wait for
   the S1 end window.
   - Each carries a provisional schema number, marked as `db_schema.py`
     already marks one, and is renumbered at the end against the final main.
   - A conflict with main's migrations on a main → integration merge is the
     contract owner's to resolve, reviewed by A.
   - Each migration runs on the production-size rehearsal, with its timing
     recorded. The total stays within the release downtime budget, and no
     migration holds the writer lock for long at Host start.
   - If provisional numbering proves impractical, the lead revisits this.
3. **Numbers:**
   - Dream attempt cap: 3.
   - Dream run deadline: 12 h, not 48 h, matching AUT-06 as signed.
   - Native-goal heartbeat window: decided in the goals conversion packet,
     from measured live heartbeat intervals (read-only counts).
   - Reaper: batches of at most 100 rows, each batch transaction under
     50 ms measured at production size. Each tick handles at most 500 rows
     and at most 200 ms of database time.

   Each conversion registers its behaviour changes as intended differences
   when it lands.
4. **Goal byte bound:** the byte bound, with `prior_steps` moved to the
   ledger, goes to the fix lane now (rf-fix, reviewer B). The condition is
   that every client-visible payload that exposes `prior_steps` or goal
   history stays byte-identical, read back from the ledger. If that is
   impossible without a payload change, the work stops and goes to the lead:
   a contract change is Peter's call. The continuation column stays in
   Phase 3.
