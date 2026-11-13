# Goal keeper

Durable goals are only as current as their owners keep them, and owners busy
with the work rarely do. On 13 Nov 2026 a goal had read "0 of 4 steps done",
step 1 "Active", checkpoint seven days old, for a week; a helper had finished
all four steps on the first day. In a replay of six real goals, even an owner
that did update its goal was right on 16 of 35 step statuses.

The Goal keeper is a built-in Janitor (role and template `goal-keeper`) that
keeps the books beside the owner. It never prompts, tells or interrupts the
agent doing the work. Like the other built-ins it is installed **paused**;
enabling or pausing it is the only switch.

## The Janitor

- **Template** `goal-keeper` (`janitors.templates`): allowed effect
  `goal_update`, recommended backend `agy`, model `gemini-4-hydrogen-low`,
  execution `{"executor": "ephemeral", "provider": "agy", "isolated": true}`,
  option `max_goals_per_run` (5, 1 to 6). Scope: all eligible agents (not
  Janitors, archived or deleted agents).
- **Triggers** (its attachments):
  - `agent-work-completed@1`, coalesce 300 s: the owner's turn ended. Its
    `coalesce_seconds` is the fold window for every trigger of a goal;
  - `goal-activity-observed@1` (new, demand kind): a helper (an agent whose
    `parent_agent_id` is the owner) sent the owner a message or ended a turn,
    a job of the owner or a helper ended, or the goal was paused, resumed,
    cancelled, blocked or superseded. A status change folds for 30 s;
  - `schedule@1`, `17 * * * *`: the sweep, which updates goals whose messages
    changed with no trigger (a long turn with no final message).
  Detaching a trigger stops what it watches.
- **Runs**: the Host-run Janitor loop (`janitor_autonomy`, every 15 s) finds
  the goals that are due and admits one run (`janitor_builtins.begin_run`,
  one per Janitor at a time). That run updates up to `max_goals_per_run`
  goals in parallel on a thread of its own, so model calls never hold up the
  loop. The run is recorded with its summary (goals updated, step changes,
  failures) and its activity: worked, idle or failed. Fold windows and retries
  are kept in the progress of the `agent-work-completed` attachment, so they
  survive a restart. A failed goal is retried once after a minute, then left to
  the sweep. A write refused because the owner changed the plan is read again
  after 15 s. Nothing new since the last update means no model call.

In every task plan, `plan.updater` shows the Janitor as that goal sees it:
`enabled` (its switch), `last_run_at` and `last_error` (its latest run), and
`goal_error`/`retry_at` (this goal's last failed update, from the progress).

## What it reads (`lib/goal_keeper.py`)

One prompt per goal, at most 100 KB:

- the goal as recorded: title, outcome, criteria, status, every step's id,
  title and status (a paused running step as "paused (was in_progress)"),
  the owner's checkpoint and the keeper's last one;
- a fact sheet from the database: the goal's status, each helper's backend,
  state, last message to the owner and last turn-end text, and recent jobs of
  the owner and its helpers. A Clarp helper's watcher job that ended with
  `worker_vanished` reads "ended (helper watcher)", not a failure;
- the owner's and helpers' messages since the cursor, tool calls as one line
  each, newest 40,000 characters.

The cursor lives in the goal (`goal.updater.through_ms`/`through_event`).
For a goal it has not seen, it starts at the owner's last checkpoint, at most
24 h back.

## The model call

`janitor_autonomy.model_json` goes through the Janitor's model and its
fallback chain (`model_fallbacks`), with no tools. The prompt states that
the record is untrusted. With `execution.isolated` (an Antigravity option, refused for any
other provider) the call is `backend.agy.isolated_run`:

- `HOME=<cache>/agy-janitor-goal-keeper-home`, a home holding agy's login and
  settings and nothing else (no GEMINI.md, no skills). That puts about 9.7k
  prompt tokens of overhead on each call instead of 28.4k (measured 9,744);
- an empty working directory, and `--sandbox --disable-slash-commands
  --print-timeout 120s --output-format json`. The process is killed after
  150 s;
- the call's conversation is deleted afterwards, along with anything older
  than an hour in that home. agy keeps every conversation (about 0.5 MB) and
  has no flag not to.

## The effect: `goal_update`

`task_plans.updater_apply`, one transaction:

- **Step statuses**, for steps that are pending, in progress or blocked. It
  never reopens a step the owner closed (completed, skipped, deferred,
  removed). "completed" needs quoted evidence; without it the step stays in
  progress with a "probably done" note. Without that guard, early completions
  happened in replay (7 on one goal).
- **The keeper's own checkpoint**, `goal.updater` `{at, progress, next_work,
  model, source_refs, changed_steps, through_ms, through_event, by:
  "goal-keeper", janitor_agent_id}`. It sits beside the owner's
  `goal.checkpoint`, which it never changes.
- **A history entry** of kind `updater`, recorded in the ledger as
  `actor_kind = system`, `actor_agent_id` = the Janitor, `delegation_id =
  goal-keeper`.

It never completes, pauses, resumes or cancels a goal, never records criterion
evidence and never changes the continuation. It does not bump the plan's
revision, so the owner's next revision-checked command is never refused
because of it. Instead it refuses its own write when the owner changed the plan
after it read.

## Paused goals and waivers

- **Pausing** keeps a running step `in_progress` with its clock stopped
  (`items[].paused`). Before, it reset the step to pending while the app kept
  showing its active time.
- **Bookkeeping on a paused goal**: the owner and the Goal keeper may record
  steps and progress. An owner checkpoint there cannot carry a continuation,
  so the goal stays paused.
- **Waivers**: the user may waive a criterion that will not be met as
  written, with a reason (`action: waive`, the apps' route only). Completion
  accepts it and the waiver stays visible. The store refuses a waiver or its
  restoring from anyone else, the owner agent included (review #311,
  finding 2).

Measured offline before this was built (six real goals, 51 events, hindsight
labels): step statuses right 238/251 (95%) against 174/251 (69%) for the
owners' own records.

## Goals paused before the install

Pausing freezes a running step's clock: `active_ms` takes the elapsed time and `started_at` is set to NULL. A goal paused before this code still has `started_at` set, so a resume, an owner's step update or the Goal keeper's write would count the whole paused span as active time. Schema step v126 (`db_migrations._migrate_to_v126`, data only) freezes those steps once at install. It freezes from the goal's last `pause` history entry, else the plan's `updated_at`, never from now. It is guarded by `started_at IS NOT NULL`, so a rerun changes nothing. Tested by `test_a_goal_paused_before_the_install_never_counts_its_paused_span` for resume, owner update and keeper write.
