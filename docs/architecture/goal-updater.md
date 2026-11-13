# Goal updater

Durable goals are only as current as their owners keep them, and owners busy
with the work rarely do. On 13 Nov 2026 a goal had read "0 of 4 steps done",
step 1 "Active", checkpoint seven days old, for a week; a helper had finished
all four steps on the first day. The owner had delegated the work and only
checkpointed. In a replay of six real goals, even an owner that did update its
goal was right on 16 of 35 step statuses: owners update in bursts at the end.

The goal updater (`server/lib/goal_updater.py`, worker `goal-updater`) keeps
the books beside the owner. It never prompts, tells or interrupts the agent
doing the work.

## When it runs

Every 15 s the worker looks at each open (active or paused) durable goal of an
agent that is not a Janitor, and at what Clarp recorded since its cursor:

- the owner's turn ended (an assistant message with `phase = final`);
- a helper (an agent whose `parent_agent_id` is the owner, started at most an
  hour before the goal) sent the owner a message, or ended a turn;
- a background job of the owner or a helper ended;
- the goal was paused, resumed, cancelled, blocked or superseded.

The first trigger opens a window. The update runs when the window is 5
minutes old (`goal_updater.fold_seconds`), folding whatever else arrived; a
status change runs it after 30 s. A sweep (`goal_updater.sweep_seconds`,
hourly) runs it for a goal whose messages changed with no trigger, such as a
long turn without a final message. Nothing new since the last update: no
model call.

One Host-wide queue runs at most five updates at once (agy takes about 220 MB
per process; at 40 at once 3 in 40 failed). A call has `--print-timeout 120s`.
A failure is logged and retried once after a minute; after that the hourly
sweep decides. A write refused because the owner changed the plan meanwhile is
retried after 15 s.

## What it reads

One prompt per update, at most 100 KB (one argv entry):

- the goal as recorded: title, outcome, criteria, status, every step's id,
  title and status (a paused running step as "paused (was in_progress)"),
  the owner's checkpoint and the updater's last one;
- a fact sheet from the database: the goal's status, each helper's backend,
  state, last message to the owner and last turn-end text, and the owner's and
  helpers' recent jobs. A Clarp helper's watcher job that ended with
  `worker_vanished` is shown as "ended (helper watcher)", not as a failure;
- the owner's and helpers' messages since the cursor, tool calls as one line
  each, newest 40,000 characters.

The cursor starts at the owner's last checkpoint (at most 24 h back) and moves
to the newest message, job end and goal event each update read.

## The model

Gemini 4 Hydrogen (low) through the Antigravity CLI: `HOME=<cache>/agy-goal-updater-home
agy --sandbox --disable-slash-commands --print-timeout 120s --output-format json
--model gemini-4-hydrogen-low`, from an empty directory. That HOME
(`backend.agy.isolated_home`) holds agy's login and settings and nothing else,
no GEMINI.md and no skills: about 9.7k prompt tokens of overhead per call
instead of 28.4k. agy keeps every conversation (about 0.5 MB) with no flag not
to, so the updater deletes each one after its call and anything older than an
hour.
It runs on the Antigravity subscription, not a paid API. The record is
untrusted: the prompt says so, the CLI gets no tool permissions and the
answer is a fixed JSON shape that can touch only step statuses and the
updater's checkpoint.

Measured offline before this was built (six real goals, 51 events, hindsight
labels): step statuses right 238/251 (95%) against 174/251 (69%) for the
owners' own records; wrong "completed" 4 against the owners' 7; about 8 s and
about 4k payload tokens per update, plus a fixed agy overhead per call.

## What it writes

`task_plans.updater_apply`, one transaction:

- step statuses, for steps that are pending, in progress or blocked. It never
  reopens a step the owner closed (completed, skipped, deferred, removed).
  "completed" needs quoted evidence; without it the step stays in progress
  with a note;
- `goal.updater`, its own checkpoint `{at, progress, next_work, model,
  source_refs, changed_steps, through_ms, through_event}`, beside the owner's
  `goal.checkpoint`, which it never changes;
- a history entry of kind `updater` (in the ledger as `actor_kind = system`,
  `delegation_id = goal-updater`).

It never completes, pauses, resumes or cancels a goal, never records criterion
evidence and never changes the continuation. It does not bump the plan's
revision, so the owner's next revision-checked command is never refused
because of it; it refuses its own write instead when the owner changed the
plan after it read.

## Paused goals and waivers

Pausing keeps a running step `in_progress` with its clock stopped
(`items[].paused`); it used to reset the step to pending while the app kept
showing its active time. The owner (and the updater) may record steps and
progress on a paused goal; a checkpoint there cannot carry a continuation, so
the goal stays paused. An owner may waive a criterion that will not be met as
written, with a reason (`action: waive`); completion accepts it and the
waiver stays visible.

## Turning it off

`goal_updater.enabled = false` in the settings, or `CLARP_GOAL_UPDATER=0` in
the Host's environment. `goal_updater.model` picks another agy model.
