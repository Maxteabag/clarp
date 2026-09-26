---
name: clarp-background-jobs
description: Register, check, finish, and cancel durable Clarp background jobs. Use for watchers or work that continues after the current response.
---

# Clarp Background Jobs

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
