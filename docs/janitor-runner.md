# Janitor runner integration

The Host owns one `lib.janitor_runner.JanitorRunner(dispatch_run)` reader. Start
and stop it with the existing Host lifecycle, alongside the ordinary schedule
runner. It does not create a service or start its own model/backend process.

`dispatch_run(run, prompt)` must use the existing durable dispatch machinery:

- Force the frozen `run.session`, `run.trace_id` as trace and client request ID,
  server-only Janitor origin, `queue_if_busy=True` and audio disabled.
- Pass the registered run identity through the runtime bridge. Validate it with
  `janitors.validate_dispatch` at actual execution and queue recovery. Merely
  providing an origin string is not authorization.
- Return `True` or `{ok: true}` only for accepted dispatch. An exception or an
  ambiguous response retries the same frozen ID after 30 seconds; it never
  replaces the payload or admits another run for that Janitor.
- Enforce quiet unread, push, speech, list-order and focus behavior separately.
  This reader does not implement presentation/notification policy.

## Stored progress and recovery

Every attachment owns its generation, source cursor, pending targets, recent
event identities, per-target review evidence and optional active run ID in
`janitor_progress`. `create_run(..., progress=...)` freezes candidates and the
consumed progress atomically before external dispatch. Model execution status and
effect receipts stay in the store's run/effect tables, never in a second queue.

Enable and event-retention gaps reconcile the current scope once and move the
cursor to the current tail. They do not replay old completed turns or missed
schedule occurrences. Compatible review evidence survives pause/enable; the
configuration store clears it when scope/trigger changes invalidate it.

The reader watches completed ordinary task turns, excluding archived agents,
Janitors and heartbeat/leader/dream maintenance. Events coalesce for eight seconds
by default, with at most three candidates per model run. A manual/other-owner
label, current work/queued work, or missing task evidence defers admission without
a model call. Identical reviewed evidence is dropped. Unchanged task backoff is
two, five and fifteen minutes; new task or phase evidence bypasses it. An optional
configured minimum run interval remains an explicit capacity limit.

Terminal state events are matched by run trace. Retained ended-turn rows provide
termination evidence when diagnostic events have expired; actual effect receipts
still decide success. A confirmed terminal run with missing/error receipts gets
one retry after 30 seconds. Repeated failure waits for changed task evidence;
unrelated agents and attachments remain eligible. Partial effects plus a failed
review are reported as an error, with the successful effects retained.

Pause/configuration generation fences are enforced by the store before progress,
dispatch and effects. The callback must perform its own execution-time guard to
cover pause between the reader's check and runtime queue admission. The reader
never cancels an observed worker or sets worker liveness.

## Local-time schedule contract

`lib.janitor_schedule.compute_next_run(cron, timezone, from_ms)` returns a strictly
later epoch millisecond instant. IANA zones are mandatory. Nonexistent local times
are skipped; repeated autumn minutes run on the first occurrence only, including
after restart. `preview_next_runs(..., count=3)` uses that same implementation.
Legacy ordinary-agent schedules keep UTC behavior. Day-of-month and day-of-week
retain the existing Host parser's AND semantics.

## Scheduled tasks

A `scheduled-prompt` Janitor ("Scheduled task") carries owner-written
`instructions` on `schedule@1`.
- **Each run is a turn in the Janitor's own session.** It uses that session's
  workspace, model and history, with origin `janitor`, so it is quiet. The prompt
  is the instructions plus the closing-report contract below.
- **One run per due occurrence, with no candidates and no label admission.** A run
  still working when the next occurrence comes due is not doubled up.
- **How a run ends.** A run whose turn ends cleanly finishes `completed`, and that
  counts as success for `janitor_failure` episodes. A turn that ended in an error
  finishes `error`.
- **Its activity sets the next occurrence.** This uses the adaptive policy below,
  with the cron's gap as the base.
- **Converting an ordinary agent's cron.** Convert the agent with
  `clarp-admin janitor create --agent SESSION --template scheduled-prompt`, giving
  its prompt as `instructions` and the same cron on `schedule@1`. Legacy agent
  schedules are UTC, so set `"timezone": "UTC"` to keep the cadence. Then remove
  the old schedule, because a Janitor refuses non-Janitor turns.

## Adaptive intervals

An interval-driven Janitor stretches its cadence while it has nothing to do.
`lib.janitor_adaptive` holds the whole policy as pure functions:

- **Every run reports its activity.** It ends `worked` (it found and did
  something), `idle` (nothing needed doing) or `failed`, plus a one-line summary.
  The values are stored on the run as `activity` and `activity_summary`.
- **Model-run Janitors report it themselves.** A task-label run is told in its
  prompt to close with
  `clarp-admin janitor outcome RUN_ID --status worked|idle|failed --summary ...`
  (`POST /janitor-runs/{run_id}/activity`). The Host-run Janitors (Label checker,
  Quota keeper, Hotseat switcher, Heartbeat keeper) record it when they complete.
- **A run that never reports counts as `worked`** and logs
  `janitorActivityMissing`. A Janitor that forgets the contract keeps its
  configured cadence; it never drifts to a day. A failed run is `failed`
  whatever it reported.
- **`advance` sets the next interval.**
  - Each idle run doubles it from the base (the configured interval, or a
    cron's gap between runs), up to the Janitor's `max_interval_seconds`
    option (default 86400).
  - `worked` resets it to the base.
  - `failed` leaves it unchanged and keeps the existing failure handling
    (retry, `janitor_failure` alerts).
  - A changed base or cap starts again from the base.
- **The Quota keeper and Hotseat switcher default to `max_interval_seconds: 0`
  (fixed).** A quota watch that backs off misses the window it guards. Set the
  option to opt in.
- **Where it applies.**
  - Task-label attachments on `schedule@1` and `active-interval@1`, and scheduled tasks: one due
    occurrence opens when the trigger fires. It closes once nothing is pending
    or running. It is `worked` if any of its runs worked, `failed` if one failed
    and none worked, otherwise `idle` (a pass with nothing to review is idle).
    For a cron, the next run is the first occurrence at least one interval after
    the occurrence began.
  - `agent-work-completed@1` and the demand triggers have no interval and are
    unchanged.
- **The state survives restarts.** It persists in the attachment's progress
  (`adaptive`) or in settings (`janitor-adaptive.<lane>`, keyed to the Janitor
  and its configuration generation).
- **`GET /janitors` and `clarp-admin janitor list`/`inspect` show the cadence.**
  Each Janitor has a `cadence` list, one entry per schedule lane:
  `{lane, base_interval_seconds, current_interval_seconds, max_interval_seconds,
  adaptive, idle_streak, last_activity, last_summary, next_run_at}`.

## Verification

```bash
python -m pytest -o addopts='' tests/unit/test_janitor_context.py \
  tests/unit/test_janitor_policy.py tests/unit/test_janitor_schedule.py \
  tests/unit/test_janitor_runner.py tests/unit/test_janitor_adaptive.py \
  tests/unit/test_janitor_activity_report.py -q
```

Tests use isolated databases or deterministic source/store adapters. They exercise
real guarded store effects, pause, queue eligibility, ambiguous delivery/restart,
no-op/no-work suppression, bounded retry, independent attachments, and Europe/Oslo
spring/fall transitions. These tests do not start services or spend model usage.
Device UX, notification policy and actual runtime bridge delivery require their
own integration checks; this test lane alone does not prove those behaviors.
