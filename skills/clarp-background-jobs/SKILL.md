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

## Provider-native tasks (Host contract 24)

The Host automatically observes Claude background `Bash` requests and their
native task receipts from transcripts bound to Clarp runtimes. Check the job's
`metadata.provider_state`: launching is only a request, running means the provider
reported a task ID, and unknown means evidence is missing or stale. A stopped
session without a completion record has unknown outcome. These jobs do not claim
PID ownership or heartbeats. A successful watcher task does not establish that a
remote deployment succeeded; inspect the actual deployment result.

Provider jobs have `can_cancel:false`: this Host cannot safely invoke Claude's
native task cancellation outside its owning provider. Do not cancel the watched
CI/deployment to stop its watcher, or signal a PID inferred from task text.
Inspect output through the job detail API; it derives the exact task output path,
limits its tail, and redacts common credential forms.

Explicit registration remains necessary for Codex code-mode tasks and arbitrary
systemd/nohup services. No broad process discovery or adoption occurs. Do not
register Claude `Agent`/`Task` helpers as background jobs. An explicitly registered
job can provide `metadata.provider`, `metadata.native_session_id`, and
`metadata.tool_use_id` matching the actual Claude Bash call to suppress the
automatic mirror in job lists/counts; never guess those IDs or deduplicate by title.
The legacy CLI does not yet populate that optional metadata automatically.
