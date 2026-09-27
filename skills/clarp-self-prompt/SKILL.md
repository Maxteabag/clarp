---
name: clarp-self-prompt
description: Send a prompt to a Clarp agent immediately or on a schedule. Use for continuations, reminders, and durable agent wakeups.
---

# Clarp Self Prompt

Use the installed helper:

```bash
clarp-admin prompt --to SESSION --text "Continue the task"
clarp-admin prompt --to SESSION --text "Check again" --delay 30m
```

For a watcher, detached/systemd/nohup worker or other process that outlives this
turn, use [clarp-background-jobs](../clarp-background-jobs/SKILL.md) for visible
purpose, status, output and cancellation; a scheduled wake alone is not tracking.

Target the intended session explicitly. Scheduled prompts are automation
messages, not messages authored by the user.

For a durable outcome commitment, prefer `clarp-goal checkpoint` with an
agent-authored timer/event continuation attached to the same goal. It atomically
retains progress, evidence and next work, fences stale wakes and exposes delivery
and recovery state. Generic scheduled prompts remain useful for reminders and
older Hosts; do not create a competing recurring loop for an enrolled goal.

Linux delayed prompts freeze a UTC calendar deadline at scheduling time, so a
service-manager reload does not postpone them. Verify the timer's actual next
expiry; successful scheduling alone does not prove delivery. Older installations
may still use relative `OnActiveSec` timers. For an authorized deadline repair,
replace only the owner's confirmed superseded timer with `OnCalendar` in explicit
UTC, preserving its action and cooldown; never dispatch a release to test a wake.
