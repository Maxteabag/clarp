---
name: clarp-background-jobs
description: Register, check, finish, and cancel durable Clarp background jobs. Use for watchers or work that continues after the current response.
---

# Clarp Background Jobs

## Registering a job does not keep it running

The registry is bookkeeping. It records state, heartbeats and cancellation; it
never supervises a process. Detach the work yourself or it dies with the turn,
and the job row is reconciled to failed.

Clarp dispatches every turn as `claude -p`. The headless docs are explicit: a
background Bash shell "is terminated about five seconds after Claude has
returned its final result and stdin has closed". So:

- `Bash(run_in_background: true)` **cannot outlive the turn that created it.**
  Its task `.output` file ends with the literal `[killed]`. Never use it for
  work that must survive the response.
- Detach from a **foreground** Bash call, via a script file, with `setsid`.
  Inline `setsid ... &` is killed by SIGUSR1 (exit 144) when the tool shell
  exits, so always launch a script file. Detaching from *inside* a background
  task does not work either — Claude Code reaps that shell's `setsid`
  descendants with it.
- Use `systemd-run --user --unit=... --collect` when the work must also survive
  a `clarp-runtime.service` restart. A `setsid` process still sits in the
  runtime's cgroup and dies with it; a transient unit gets its own.
- `claude --bg` is unavailable here: it cannot be combined with `-p`.

Detach for survival, then register for visibility. Two separate steps; you need
both. When work goes missing, check the task `.output` for `[killed]` before
blaming a server restart — the turn boundary is the usual cause.

## Registry

Use the installed helper at
`clarp-agent-bg`.

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
