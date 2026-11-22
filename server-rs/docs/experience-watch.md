# Experience watcher

The Rust Host measures the user's experience from its own state. It reads
a single time window across the checks and never runs a test or calls a
model. The effective window covers at least the configured pass interval
and stretches to the preceding pass when a pass is delayed. A failed read is logged and does not clear findings for that source.

Set this section in the Rust Host's `config.toml`. The watcher is off by
default. The first pass waits one interval, and subsequent passes reread
the file.

```toml
[experience_watch]
enabled = false
interval_seconds = 300
summary_hour = 9
front_desk = "SESSION"
release_kinds = ["check", "test"]

[experience_watch.owners]
app_crash_ios = "SESSION"
app_crash_android = "SESSION"
queue = "SESSION"
skills = "SESSION"
cards = "SESSION"
release = "SESSION"

[experience_watch.thresholds]
wait_seconds = 60
wait_count = 1
queue_stuck_seconds = 600
crash_events = 10
window_minutes = 15
failed_sends = 1
card_age_hours = 24
release_hold_minutes = 10
```

`clarp-admin experience [--since 1h] [--open] [--json]` reads stored
findings even while the watcher is off. It reports late reads (steered,
waited, or still waiting), paused or stuck queues, client crash signals, failed
or refused user sends, unhealthy release skill links, old decision cards,
and long-running jobs that hold a release. An authenticated device sending
`X-Clarp-App-State: foreground` counts as one observed foreground session
until the Host sees `background` from that device. Background requests and
repeated foreground requests add no launches. If a client sends no marker,
requests to `POST /devices` and `GET /server-info` are shown separately as
unmarked context. These endpoints also serve background sync and reconnects.
An app-crash finding requires a crash-kind client event or a MetricKit
report with a nonzero crash count. A malformed `X-Clarp-Client` records
an unknown platform and is logged once per paired device per Host run.
The HTTP counters keep the last 24 hours, up to 32,768 events per signal,
and restart with the Host. Send history
is rebuilt from the actor journals. The Rust queue has no separate
`launch_unknown` status. Recent unconfirmed interruptions count in sends.

A finding stays open until a pass stops seeing it. Cleared findings are kept
for seven days and up to 2,000 rows. The configured owner and front desk get
one notice on open and on clear, from `experience-watch`. A shared recipient
gets one copy. `wait` and `sends` use the `queue` owner unless assigned
separately. `app_crash_ios` and the other platform kinds fall back to
`app_crash`. The front desk gets one summary during the configured local
`summary_hour`. A missed hour is skipped. Notices and the admin command
show the paired device name with its ID, or the agent's name and session.
Timestamps use the Host's local timezone, with a UTC offset. JSON keeps
the stable subject IDs and original epoch-millisecond timestamps.

A release finding requires a running job registered as `check` or `test`
and a recognized check step in that job's live worker process tree. Builds,
releases, installs and opaque remote commands are excluded. The detail
shows the registered kind, active step and Host-local step start time.
