# Experience watcher

The Rust Host measures the user's experience from its own state. It reads
a single time window across the checks and never runs a test or calls a
model. A failed read is logged and does not clear findings for that source.

Set this section in the Rust Host's `config.toml`. The watcher is off by
default. The first pass waits one interval, and subsequent passes reread
the file.

```toml
[experience_watch]
enabled = false
interval_seconds = 300
summary_hour = 9
front_desk = "SESSION"
release_kinds = ["release", "install", "check", "test"]

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
and long-running jobs that hold a release. Relaunch requests are counted
by authenticated device and include the latest reported app build and
contract. An app-crash finding requires a crash-kind client event or a
MetricKit report with a nonzero crash count. Launch requests are context
only, so repeated pushes, reconnects, and background sync cannot open a
finding on their own. The HTTP counters keep at most
32,768 recent events per signal and restart with the Host. Send history
is rebuilt from the actor journals. The Rust queue has no separate
`launch_unknown` status. Recent unconfirmed interruptions count in sends.

A finding stays open until a pass stops seeing it. Cleared findings are kept
for seven days and up to 2,000 rows. The configured owner and front desk get
one notice on open and on clear, from `experience-watch`. A shared recipient
gets one copy. `wait` and `sends` use the `queue` owner unless assigned
separately. `app_crash_ios` and the other platform kinds fall back to
`app_crash`. The front desk also gets one local-time summary per date. Notices and the
admin command show the paired device name with its ID and use the Host's
local timezone, with a UTC offset. JSON output also keeps the original
epoch-millisecond timestamps.
