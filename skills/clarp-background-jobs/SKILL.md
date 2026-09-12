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
