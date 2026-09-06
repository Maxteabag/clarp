# Activity-aware Janitors and reset defaults

`active-interval@1` is a reusable interval trigger for compatible scheduled
maintenance jobs. Task labels currently supports it; demand-only explainers and
delegators keep their demand contracts. This does not create or enable a Claude
account-recovery Janitor automatically.

Parameters: `interval_seconds` (900), `idle_timeout_seconds` (300),
`run_on_resume` (true), and the existing bounded `max_targets`/coalescing controls.
Seconds accept 60–86400. Check-on-return remains interval-throttled: repeated
focus changes never produce a burst. Inactivity discards pending candidates and
clears the next deadline; returning reconciles current evidence, not missed ticks.
Already-running work finishes normally. Queued work is rechecked before execution.

Full-auth `POST /application-activity` reports a per-process UUID, monotonic
sequence, foreground boolean, input age in milliseconds and sending timestamp.
Leases expire after45 seconds and reject stale/future packets. Client reporting
is independent of push-alert preferences. C++ observes window/input/logind state;
iOS observes scene state, taps, scrolling and editor-change notifications. A
background app, revoked device, unknown lock state or expired lease cannot wake
maintenance. No transcript, keystroke content or credentials enter this store.
Per-client leases and per-trigger progress are SQLite-owned.

`clarp-admin janitor reset-defaults SESSION --expected-revision N` restores
attached trigger parameters, known typed job options and compatible model/executor
defaults. It preserves identity, history, backend and watched-agent scope, and
pauses/fences the Janitor. Explicit enablement is still required. The iOS inspector
exposes the same guarded operation behind a confirmation.

Schema77 registers the trigger after built-in Janitors' schema76. No existing
Janitor is automatically converted or enabled. Matching Host/client rollout is
needed for activity reporting; old Hosts harmlessly ignore unsupported reports.
Robot appearance follows the existing built-in rusty metal portrait family and
the updated creation skill; existing custom portrait assets are not overwritten.
