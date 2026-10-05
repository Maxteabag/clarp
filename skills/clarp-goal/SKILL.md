---
name: clarp-goal
description: Own a durable outcome across turns with acceptance criteria, explicit limits, an adaptive working plan, evidence and reliable continuation. Use for substantial authorized work, recovery, or progress that must survive interruptions.
---

# Clarp Goal

Use `clarp-goal` as the single lifecycle entry point. This upgrades the existing
task-plan artifact; `clarp-agent-tasks` and `clarp-tasks` remain compatibility
routes. A goal is the outcome commitment. Its plan is your current strategy.
The native provider's goal loop is a separate execution capability, not the
Host commitment. Host recovery yields while a native goal owns continuation.

Verify your session/native binding using `clarp-sessions` before creating work.
State the authorized outcome, evidence needed to establish it, explicit limits,
and a useful initial strategy. Choose steps, collaborators and timing yourself.
Do not turn an interactive HTML planning proposal into execution authorization.

```sh
clarp-goal create SESSION ALIAS "Outcome" \
  '[{"id":"probe","title":"Probe the actual behavior"}]' \
  '{"outcome":"Deliver the requested outcome","criteria":["Observable acceptance result"],"limits":"No publication","enroll":true}'
clarp-goal list SESSION
clarp-goal get RETURNED_PLAN_ID
```

Keep the returned plan ID and revision; aliases are not global identifiers.
Creating another commitment does not cancel this one. Explicitly select the
intended goal for every change. Conflicts require reloading and reassessing;
never blindly retry with a newer revision.

```sh
clarp-goal step PLAN_ID REVISION probe in_progress
clarp-goal checkpoint PLAN_ID REVISION \
  '{"progress":"Probe found an existing path","next_work":"Inspect the new results and choose the next action","continuation":{"kind":"timer","due_at":EPOCH_MILLISECONDS}}'
clarp-goal replan PLAN_ID REVISION \
  '{"reason":"Existing path makes the first approach unnecessary","steps":[{"id":"reuse","title":"Validate and reuse the existing path"}]}'
```

Replan freely within scope: add, split, reorder, replace or retire methods.
Retired work remains visible with its reason and is never counted accomplished.
Required skipped/deferred work still needs resolution. Original acceptance
criteria remain intact even when the method changes. Record meaningful changes,
not every tool call. Wake prompts carry the current checkpoint as provisional
context: reassess fresh conditions rather than mechanically replaying old text.

Checkpoint, criterion evidence and a wake/dependency are persisted together:

```sh
clarp-goal checkpoint PLAN_ID REVISION \
  '{"progress":"Build launched","next_work":"Inspect its result","continuation":{"kind":"dependency","key":"build-unique-id","reason":"Waiting for build","due_at":TIMEOUT_EPOCH_MILLISECONDS}}'
clarp-goal dependency PLAN_ID REVISION \
  '{"key":"build-unique-id","outcome":"succeeded","evidence":"Exact build result/log path"}'
# Use outcome "failed" for an actual failure; never invent success after timeout.
```

Track detached workers (including systemd/nohup and watchers) with
[clarp-background-jobs](../clarp-background-jobs/SKILL.md), so purpose, status,
output and cancellation remain visible while this goal waits.

A worker must retain the goal identity/revision and unique dependency key. A stale
callback is rejected; reload to reconcile it rather than overriding a newer plan.
A lost callback times out into owner reconciliation. Scheduling is not delivery:
inspect continuation state/history for the admission receipt, failures and retry
state. A running checklist row alone is not a live process.

```sh
clarp-goal checkpoint PLAN_ID REVISION \
  '{"progress":"All acceptance checks passed","next_work":"Close with evidence","evidence":{"criterion-1":"Exact probe result"}}'
clarp-goal complete PLAN_ID REVISION
clarp-goal pause PLAN_ID REVISION '{"reason":"User paused this goal"}'
clarp-goal resume PLAN_ID REVISION '{"reason":"User explicitly resumed this goal"}'
clarp-goal cancel PLAN_ID REVISION '{"reason":"User cancelled the outcome"}'
```

Completion requires evidence for every criterion and resolution of required work;
a final answer or all-terminal checklist is insufficient. Preserve the original
goal when answering side questions. Respect explicit stops and pending approvals;
never self-approve or resume user-paused work without renewed authorization.
Recovery uses bounded backoff and stops for attention when its configured attempt
budget is exhausted. Choose explicit blockers or agent-authored timer/event waits
when appropriate; no fixed self-prompt cadence is required.

Historical plans remain readable and unenrolled. For an unfinished historical
plan, explicitly `enroll PLAN_ID REVISION JSON_GOAL` with the actual outcome,
criteria, limits and `enroll:true`; do not infer missing evidence or mass-enroll
old commitments. On an older Host without `clarp-goal`, use `clarp-tasks` plus
`clarp-self-prompt`/background jobs and retain honest checkpoints until upgraded.

## Subgoals and the ledger

Give a goal explicit subgoals when the outcome has parts with their own reason and
evidence. They are retired, never deleted, and each change needs the subgoal's
current revision:

```sh
clarp-goal subgoal PLAN_ID add \
  '{"subgoal_id":"phone","title":"Works on Peter's phone","intent":"The original acceptance","criteria":["Real events from his play"]}'
clarp-goal subgoal PLAN_ID update \
  '{"subgoal_id":"phone","expected_revision":1,"fields":{"current_action":"Waiting for build 2741"},"reason":"Install not verified"}'
clarp-goal ledger PLAN_ID
```

Every goal change you make is also kept in the append-only ledger with its
reason and the state before and after; `ledger` reads it. If a bookkeeping
delegate keeps your books (`clarp-goal-bookkeeping`), its observations and
claims appear there too, marked as its own; they never complete or change your goal.

## Working context belongs to the goal

Keep evolving instructions, architecture reasoning, constraints and next-slice
findings in named Markdown documents or JSON records. Their internal structure
is yours; no fixed outline or database migration is needed.

```sh
clarp-goal document PLAN_ID PLAN_REVISION \
  '{"name":"working-notes.md","format":"markdown","content":"# Current reasoning\nReassess the latest probe; do not redo completed work.","document_revision":0,"reason":"Initial findings"}'
clarp-goal read PLAN_ID working-notes.md
# Read a historical document revision:
clarp-goal read PLAN_ID working-notes.md 1
```

For JSON, use `format:"json"` and put any JSON value in `content`.
`document_revision:0` creates; later writes require the exact current revision.
A checkpoint may include a `documents` array of these same records, atomically
saving notes, evidence, next work and continuation. Plan and document conflicts
abort the entire transaction. `get` lists names/revisions; wake prompts include
that index so the owner can retrieve the current context selectively.

The database is authoritative. If exporting a working copy, retain its goal,
name and revision alongside it, then explicitly synchronize with that revision.
A stale copy must be reconciled, never silently overwrite a newer document.
Documents are limited to 64 KiB each: reference managed files/assets for large
logs, media and build evidence. Database persistence survives process/Host
restarts; it is not replication, backups or recovery after losing the Host disk.

For an existing Clarp background job, include its generation-specific
`job_handle` in the dependency continuation, alongside the unique key and timeout.
The Host verifies matching goal owner and job generation, then observes terminal
success/failure directly. A replaced or missing worker prompts reconciliation;
its status never automatically supplies criterion evidence or completes the goal.

If the owner's native conversation intentionally changes, recovery stops. Verify
the new binding with `clarp-sessions`, pause the goal, then explicitly use
`rebind PLAN_ID REVISION '{"native_session_id":"VERIFIED_ID","reason":"Why the conversation changed"}'`.
This retains the outcome/criteria/history and leaves the goal paused; resume only
within current authorization. A stale wake can never silently choose a new owner.

`next_work` can carry your dynamically composed continuation instructions,
including what changed, current constraints, and work that must not be repeated.
It remains provisional context on the next wake. Optional nonblocking questions
may coexist with authorized independent work; approvals and questions explicitly
marked as blocking still prevent automatic continuation.

A manually tracked goal (`enroll:false`) never claims an automatic wake. To opt
it in later, verify its owner binding and use
`enroll PLAN_ID REVISION '{"enroll":true,"reason":"Owner opted into recovery"}'`.
Existing outcome, criteria, evidence and documents stay intact.
