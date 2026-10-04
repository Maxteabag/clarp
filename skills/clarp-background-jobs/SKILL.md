---
name: clarp-background-jobs
description: "Track background work that outlives a turn: watchers, detached workers, systemd services and nohup processes. Give the user visible purpose, status, output and cancellation through durable Clarp jobs."
---

# Clarp Background Jobs

A detached process needs a visible purpose, current status, inspectable output and
a working cancel action. Service supervision alone does not provide this tracking.

Use the installed helper at
`clarp-agent-bg`.

```sh
clarp-agent-bg SESSION job-upsert STABLE_ID KIND "Job title"
clarp-agent-bg SESSION job-heartbeat RETURNED_HANDLE
clarp-agent-bg SESSION job-active RETURNED_HANDLE
```

`KIND` is required (for example `implementation` or `research`). Reuse the
generation-specific handle printed by `job-upsert` in subsequent operations.

Register one stable job id per independently cancellable target. Check
the generation-specific handle printed by `job-upsert` with `job-active`
before delivery or another irreversible action, and call `job-heartbeat` at
least every two minutes while it is live. Call `job-finish <handle>` for
success or `job-fail <handle> <reason>` for failure. Managed message-watch
workers are recognized automatically. Other detached workers must set
`CLARP_BACKGROUND_WORKER_PID` to their stable worker PID when invoking the
helper if they want process-identity fencing; all workers still heartbeat.
An adopted worker that exits without a terminal update is reconciled to failed
instead of remaining Running forever.
Cancellation is sticky for a stable ID; use explicit `job-restart` only when
the user intentionally starts a new run of that previously cancelled target,
then use the new handle it prints.

Job ids are global across agents, not per session: registering an id another
agent owns fails with `job id already belongs to another agent`. When you take
over a service from another agent, give it a new, distinct stable id under
your own session (e.g. prefix it with your service name) unless you are truly
continuing that agent's ownership. Never register or heartbeat as the previous
owner, and never edit the job database to move an id. Before switching traffic
to the new instance, start it for real and confirm its `job-upsert` succeeded
and its handle is active; if registration fails, keep the old instance serving.

A job is a **background process**. A Clarp helper agent you created is a
**sub-agent** and is counted separately (see `clarp-sub-agents`); do not
register a job just to make a helper look busy.

Make the process inspectable (`GET /background-jobs/<job_id>` shows the
timeline, progress, owner links and log tail):

```sh
clarp-agent-bg SESSION job-progress RETURNED_HANDLE "running tests 3/10"
clarp-agent-bg SESSION job-log RETURNED_HANDLE /var/tmp/my-worker.log
clarp-agent-bg SESSION job-cancel RETURNED_HANDLE
```

`job-progress` stores one short line on the job and pushes it to the apps;
send it when something meaningful changes, not every second. `job-log`
registers an absolute log path. The Host shows its last 64 KB only when the
resolved file is a regular file under the directory you ran the command
from, your agent's cwd, or `/var/tmp`. `job-cancel` lets the owning session
close its own job, for example one whose worker was stopped or died, without
the worker's PID. `job-finish` and `job-fail` stay fenced to the registered
worker. A job whose recorded worker PID has exited is failed automatically
(`worker_vanished`) without waiting for the heartbeat timeout.

## Unavailable status is not cancellation

`job-active` returns **0** for confirmed active ownership, **1** for a
confirmed terminal job or superseded worker/generation, and **2** when state
cannot be established (including a missing job or database error). Treat timeouts,
missing executables and other unexpected exit codes as unknown too. Older helpers
may return 1 on an exception; update the helper before depending on this contract.

On unknown state, retain the same handle, log the uncertainty, pause consequential
deliveries and retry with bounded backoff (for example 5, 10, 20, 40, then 60 seconds).
Do not register a new generation to bypass an unreadable status. Resume delivery
only after a fresh successful ownership check. Exit cleanly on confirmed terminal
or superseded ownership so `Restart=on-failure` does not revive cancellation.

Persist processed-item state only after confirmed delivery acceptance. Use a stable
idempotency key across retries and restarts where the destination supports it;
otherwise reconcile uncertain acceptance before retrying. Preserve a fixed watch
boundary across restarts so items arriving during downtime remain eligible.

## Completion that must wake you

A Claude background `Bash` task belongs to the turn that started it. Clarp runs
one Claude process per turn; when your reply ends, that process exits and the
task stops with it, so "you will be notified when it completes" does not hold
here. A process you start detached keeps running, but nobody hears that it
finished. On 2026-10-04 a detached render finished in two minutes, its watcher
had ended with the turn, and the user polled 45 minutes later.

When the result must reach you after this turn, keep four things separate:
the work itself, the worker that watches it, the worker's durable receipt,
and the wake that brings you back.

1. Have a goal for the outcome (`clarp-goal create`, see `clarp-goal`).
2. Start the work through the worker, `scripts/run_detached_job.sh` in this
   skill's directory (for example
   `~/.claude/skills/clarp-background-jobs/scripts/run_detached_job.sh`). It
   registers the job under its own PID and prints the handle:

   ```sh
   H=$("$SKILL_DIR/scripts/run_detached_job.sh" "$SESSION" render-intro-2 render \
       "Render intro video" /var/tmp/intro/render.log -- ffmpeg -i in.mov out.mp4)
   ```

   A job id names one run: give a new run a new id. A cancelled id is refused
   (the script exits non-zero and runs nothing); use `job-restart` only for a
   deliberate rerun of that same target. The worker writes
   `/var/tmp/intro/render.log`, `render.log.exit` with the exit status, and
   reports `job-finish` (exit 0) or `job-fail` (anything else) itself. A
   cancel stops its command.
3. Attach the dependency before you reply, while the handle is in hand:

   ```sh
   clarp-goal checkpoint "$PLAN" "$REV" "{\"progress\":\"Render started\",
     \"next_work\":\"Read render.log.exit and the log before delivering\",
     \"continuation\":{\"kind\":\"dependency\",\"key\":\"render-intro-2\",
     \"job_handle\":\"$H\",\"reason\":\"Waiting for the render\",
     \"due_at\":$(( ($(date +%s) + 3600) * 1000 ))}}"
   ```

When the job ends, goal recovery records it as the dependency result
(`outcome` is how the job ended, `outcome_state` what is known of the work,
`terminal_reason` why) and dispatches a wake; the goal history then shows
`dependency_result`, `wake_claim`, `dispatch_admitted` and, once your turn
runs, `execution_observed`. A worker that dies without a receipt reads
`outcome: failed`, `outcome_state: unknown`, `terminal_reason:
worker_vanished`: the work may or may not have finished, so look before
saying either. If the deadline passes first you are woken to inspect it. On waking,
read the exit file and the output yourself: the job status says how the
worker ended, not that the result is right.

That wake depends on goal recovery running on this Host. The goal's
`continuation.observed_state` reads `host_paused` when autonomous wakes are
paused (`CLARP_HEARTBEATS_DISABLED`), and `blocked`, `attention` or
`owner_changed` when it cannot wake you. Then nothing will: tell the user so,
say where the result will appear (job detail, the exit file), and check it
yourself on the next turn. Never describe a registered job or a scheduled
check as a promise that you will report back.

## Provider-native tasks (Host contract 24)

The Host automatically observes Claude background `Bash` requests and their
native task receipts from transcripts bound to Clarp runtimes. Check the job's
`metadata.provider_state`: launching is only a request, running means the provider
reported a task ID, and unknown means evidence is missing or stale. A stopped
session without a completion record has unknown outcome. When the Claude turn that
launched a task exits, the task is shown stopped right away
(`provider_turn_exited_without_result`), fenced to that exact process: a
later turn on the same conversation is not affected. That stop is provisional;
the provider's own completion record, if it arrives later, replaces it. These jobs do not claim
PID ownership or heartbeats. A successful watcher task does not establish that a
remote deployment succeeded; inspect the actual deployment result.

Provider jobs have `can_cancel:false`: this Host cannot safely invoke Claude's
native task cancellation outside its owning provider. Do not cancel the watched
CI/deployment to stop its watcher, or signal a PID inferred from task text.
Inspect output through the job detail API; it derives the exact task output path,
limits its tail, and redacts common credential forms.

Explicit registration remains necessary for Codex code-mode tasks and arbitrary
systemd/nohup services. No broad process discovery or adoption occurs. Do not
register Claude `Agent`/`Task` helpers as background jobs. The Clarp PreToolUse hook attaches non-secret invocation identity to background
Bash commands through updatedInput, without making a permission decision. The
legacy helper automatically retains that identity only when the registering owner
matches its live native binding, so its actual registration replaces the automatic
mirror in job counts. This requires the updated plugin and Host code together.
An explicitly registered job can also provide `metadata.provider`, `metadata.native_session_id`, and
`metadata.tool_use_id` matching the actual Claude Bash call to suppress the
automatic mirror in job lists/counts; never guess those IDs or deduplicate by title.
Older plugin installations and sanitized service environments may lack this
provenance; do not guess an identity when it is absent.
