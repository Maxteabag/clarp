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
scroll_flash = "SESSION"
queue = "SESSION"
skills = "SESSION"
cards = "SESSION"
release = "SESSION"

[experience_watch.thresholds]
wait_seconds = 60
wait_count = 1
stalled_turn_minutes = 10
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
is rebuilt from the actor journals. Wait findings count fresh user and
Oracle messages. Agent coordination, scheduled sends, and synthetic form
deliveries are excluded. The Rust queue has no separate
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

A chat-view scroll flash is a `scroll_flash` client event (below). One finding
per device: its first flash opens `scroll_flash_<platform>` (`ios`, `android`,
`desktop` or `web`) with the device as subject. The watcher reads each device's
flashes of the last 24 hours, whatever the pass's window: the finding reads
"N chat-view scroll flashes since HH:MM on <platform>; the newest at HH:MM:
<rule>, with <cause kinds>.", new flashes update it, and its recipients hear of
what changed at most once an hour while it stays open. It clears 24 hours
after the device's newest flash. Its owner is `owners.<kind>`, else
`owners.scroll_flash`, else that platform's app-crash owner
(`app_crash_<platform>`, else `app_crash`), and the front desk as for every
finding.

The apps' scroll dashcam sends `scroll_flash` (level `fault`) to `POST /clog`
when a chat view jumps and comes back without a gesture. Its `detail` is
`{"chat": session, "window": pane, "screen": viewport height px,
"rule": "jump_back" | "top_flip", "frames": [{"t": ms, "y": offset px,
"h": content height px, "top": top visible message id, "g": gesture}]
(changed frames only, at most 60), "events": [{"t": ms, "kind": "reload" |
"insert" | "remove" | "height" | "live", "note": short}] (at most 30)}`.
The Host keeps it like an important event (7 days, with its context) and
lets its detail run to 16 KB (4 KB for other events).

A release finding requires a running job registered as `check` or `test`
and a recognized check step in that job's live worker process tree. Builds,
releases, installs and opaque remote commands are excluded. The detail
shows the registered kind, active step and Host-local step start time.

The Host marks a stalled turn after `stalled_turn_minutes` without tool
output while a user or Oracle message waits. It reads this threshold at
startup. Clients can offer Stop through the stall fields even when this
watcher's notices are disabled. With notices on, the queue owner and
front desk are told about stall findings by the watcher.
