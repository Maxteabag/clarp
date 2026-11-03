# Lifecycles

Status: accepted 2026-11-03 as part of the server refactor program
(`REFACTOR.md`, section 3, "Lifecycle kit"). Owner: Peter; kept by the
contract owner. It extends rules 1, 4 and 6 of `state-and-boundaries.md`.

A **lifecycle** is a long-lived thing with a state that changes over time and
must not get stuck: a turn, a CLI process slot, a goal, a background job, a
voice claim, a login task. The review behind the program (findings cited by id
at `09ee9279`) traced most of its high findings to lifecycles that break one of
the four properties below: two writers racing, a check and a write in separate
statements, a state nothing ever leaves, or a side effect that runs before the
decision it depends on has committed.

The kit is for lifecycles only. Request handling, parsers, importers and config
do not use it.

## The four properties

1. **One owner.** Exactly one module writes the state. Everyone else asks the
   owner with an event (`transition(..., "cancel")`) and never writes the state
   column, sets the in-memory field or deletes the row themselves. The
   status-column writer guard enforces this for database-backed lifecycles.
2. **Atomic compare-and-set transitions.** A transition reads the state,
   decides and writes in one transaction, and the write names the state it
   expects:

   ```sql
   UPDATE <table> SET <state> = ?, <entered_at> = ? WHERE <key> = ? AND <state> = ?
   ```

   Zero rows means somebody else moved the row first: a `Conflict`, which the
   caller handles explicitly (usually by re-reading and deciding again, or by
   doing nothing because the other writer already did the work). It is never a
   silent overwrite and never a blind `UPDATE ... WHERE key = ?`. In memory the
   same rule holds under the owner's lock: compare, decide and assign without
   releasing it.
3. **A deadline on every non-final state.** Every state that is not final
   declares either a `Deadline(after_ms, expire_to=<final state>)` or a
   `WaitForever(reason)`. A reaper moves rows past their deadline to the final
   state with the reserved event `deadline_expired`, through the same
   compare-and-set. `WaitForever` is for states that legitimately wait on a
   person (a decision the user has not answered); the reason says who ends it.
   "It will finish eventually" is not a reason.
4. **Decide, then act.** The decision is a pure function of the current state
   and the event (`Lifecycle.decide`), taken under the lock or inside the
   transaction. Effects (SSE broadcasts, pushes, killing a process, starting a
   backend, writing files) are queued in the transaction's `Outbox` and run only
   after `COMMIT`. A rolled-back transaction drops its outbox, so no effect ever
   announces a state that did not commit. An effect that fails does not undo the
   committed state; every effect runs and the failures surface together as
   `EffectsFailed`.

## The API (`server/lib/lifecycle.py`)

The kit is stdlib only and imports nothing from the server, so any layer may
use it. It is not used by production code yet; conversions happen in Phase 3.

```python
from lib import lifecycle as lk
from lib.lifecycle import Deadline, Edge, Lifecycle, WaitForever

JOB = Lifecycle(
    name="background_job", owner="lib/background_jobs.py",
    initial="queued", final=frozenset({"done", "failed", "cancelled"}),
    edges=(
        Edge("start",  frozenset({"queued"}), "running"),
        Edge("finish", frozenset({"running"}), "done"),
        Edge("cancel", frozenset({"queued", "running"}), "cancelled"),
    ),
    waits={"queued": Deadline(10 * 60_000, "failed"),
           "running": Deadline(30 * 60_000, "failed")},
    table="background_jobs", key="job_id",
    state_column="status", entered_column="status_at",
)

with lk.transaction(con) as outbox:          # BEGIN IMMEDIATE ... COMMIT
    moved = lk.transition(con, JOB, job_id, "finish", now=now_ms())
    outbox.add(lambda: broadcast(job_finished(moved)))   # runs after COMMIT
```

| Name | What it does |
|---|---|
| `Lifecycle(...)` | The declaration. Construction validates it and raises `LifecycleError` listing every problem: a non-final state without a wait, a deadline that does not end in a final state, an edge out of a final state, an event with two targets from one state, a state unreachable from `initial`, a state that cannot reach a final state, a bad SQL identifier. Leave `table`, `key`, `state_column` and `entered_column` empty for an in-memory lifecycle. |
| `Lifecycle.decide(state, event)` | Pure. The target state, or `IllegalTransition`. |
| `Lifecycle.deadline(state, entered_at)` / `overdue(state, entered_at, now)` | Pure. When the state expires; `None` for final and `WaitForever` states. |
| `lk.transaction(con)` | `BEGIN IMMEDIATE`; yields an `Outbox`; `COMMIT` then runs the outbox, or `ROLLBACK` and drops it. Refuses to nest inside an open transaction. |
| `lk.create(con, lc, key, now=, values=)` | Inserts the row in `initial`. |
| `lk.transition(con, lc, key, event, now=, values=)` | Reads the state, decides, compare-and-sets. `IllegalTransition` or `Conflict`. |
| `lk.compare_and_set(con, lc, key, event=, source=, target=, now=, values=)` | The raw CAS, for owners that decided already (for example from a batch). |
| `lk.reap(con, lc, now=)` | Moves every row whose state's deadline has passed to the deadline's final state. Returns the transitions so the caller can queue their effects. |
| `Outbox.add(fn)` / `Outbox.run()` | After-commit effects; usable on its own by in-memory owners. |

Every write sets `entered_column` to `now`, so deadlines count from the moment
the row entered its state. Writes outside `lk.transaction` are refused.

Each owner declares its lifecycle as a module-level `Lifecycle(...)` constant,
so tests can find every declaration by scanning `server/` without a registry.

## Guard tests (ratchet mode)

- `tests/unit/test_lifecycle_guards.py`: every status column in the schema
  (`status`, `*_status`, `*_state`, `phase`, plus `state_log.kind`) belongs to
  a declared lifecycle or is listed in `UNCONVERTED` with the lifecycle it will
  become. Columns that are not lifecycles (free text, append-only logs) are
  listed in `NOT_LIFECYCLES` with the reason. Every declared lifecycle passes
  its own validation, so each of its non-final states has a deadline or a
  reason to wait forever. A new status column, a stale entry or a longer
  `UNCONVERTED` list fails.
- `tests/unit/test_table_writers.py`: a status column is written only by its
  lifecycle's owner. Today's writers start in `STATUS_WRITERS`; an entry that
  no longer matches a real writer, or that a declared lifecycle now owns,
  fails, so the list only shrinks. An `UPDATE` with a dynamic `SET` (or an
  `INSERT` with a dynamic column list) counts as writing every status column
  of its table.

## Lifecycles to convert (Phase 3)

The order follows severity. Each conversion is its own slice: declare the
lifecycle, route every writer through the owner, add the reaper, and move the
effects into the outbox. Behaviour changes that the conversion implies (a
deadline that did not exist before) are registered intended differences.

| Lifecycle | Owner today | Where the state lives | Property broken | Findings |
|---|---|---|---|---|
| Turn and CLI process slot | `turn_lifecycle.py`, `turn_slots.py`, `turn_dispatch.py` | `state_log`, `turns`, in-memory slots | 2: `transition()` checks and writes in separate autocommit statements across processes, settlement not atomic with the state row; 4: preemption spawns the next process before the SIGTERMed one exits; 3: `turns.ended_at` never set on normal completion | TP-01, TP-10, TP-11 |
| Runtime release | `runtime_release.py`, `runtime_bridge.py` | in memory (`phase`) | 2: the idle fast path seals while provider turns or unpersisted work still run; 4: an error in the seal path leaves the admission fence up | TP-04, TP-06 |
| Goal | `task_plans.py`, `task_goal_recovery.py` | `task_plans`, `agent_goals` | 2: Stop reads the revision outside the transaction and the pause is rejected and swallowed; 3: a stale native `agent_goals` row blocks Host goals forever | AUT-01, AUT-20 |
| Background job | `background_jobs.py` | `background_jobs` | 3: expiry is wall-clock heartbeat age only, so suspend or a clock jump fails live workers | AUT-03 |
| Codex client | `codex_app_server.py` | in memory (shared pool) | 1: one bad notification kills the shared reader yet leaves the process in the pool; 3: a credential change never reaches a client that is always busy | BACKENDS-02, BACKENDS-06 |
| CLI process | `backend/*.py`, `process_registry.py` | in memory (process handles) | 1: five drain loops handle a crash five ways, some never report an outcome; 3: interrupt never escalates to SIGKILL | BACKENDS-04, BACKENDS-07 |
| Voice claim and Live call | `oracle_live_stable.py`, `oracle_live.py`, `agent_rings.py` | in memory (`LIVE_PHASES`, claims) | 4: an early failure skips `release_connection`; 3: stale Live phases never expire and block new calls | RTO-01, RTO-04 |
| TTS clip | `tts_worker.py`, `tts_queue.py` | `tts_queue`, `clips` | 4: fallback appends a second clip to a stream that already carried bytes; 3: restart re-queues synthesizing rows with no age limit | VP-01, VP-05 |
| Account park | `account_failover.py` | in memory (`parked_since`) | 3: the 30-minute park cap restarts on every resumed turn | BACKENDS-05 |
| Dream round | `dreaming.py` | `dream_rounds`, `dream_runs` | 3: a failed round is re-queued forever and blocks future dreams | AUT-06 |
| Janitor run | `janitor_runner.py` | `janitor_runs` | 3: task-label and scheduled-prompt runs have no TTL | AUT-15 |
| Login task | `backend_auth.py` | in memory (`_tasks`) | 3: a task can stay `running` forever and block every later sign-in | BACKENDS-17 |

The other status columns in the schema are listed as unconverted in the guard
test; they convert when a slice touches them, not as a program of their own.
