# Heartbeat keeper, Quota keeper, Hotseat switcher, and global Janitor models

The Host installs two paused, persisted Janitor identities: `heartbeat-decider`
and `quota-monitor`. The Hotseat switcher (`account-hotseat`) is an optional
template in the same catalog: create it on a Host that has the `hotseat` CLI. Enable/configure them through the existing Janitors UI or
`clarp-admin janitor`; their enabled state and options belong to the identity.
The Host lifecycle owns one `AutonomyJanitors` service. No agent-created timer or
separate credential watcher is required.

## Intelligent continuity

The Heartbeat keeper calls the selected real one-shot model adapter with current
plan items, pending team context, recent messages and active jobs. Its JSON response
chooses wake/defer/noop, continuation text and the next review time. A short
service polling interval is a scheduling mechanism, not a fixed wake cadence.
Hard ownership gates remain outside the model: opt-in target, no user-stop,
no paused/occupied queue, fresh conversation/source revision, current Janitor
scope/generation. The exact proposal is persisted before dispatch and carries
one stable client request ID. The runtime validates that proposal again before
admission. A model failure records failure and backs off without waking anyone.

After explicitly adopting the keeper, pausing it does not reactivate the former
rigid periodic loop. Restart continuity remains separate. No archived, internal,
Janitor or user-stopped target is eligible. Changing durable evidence can trigger
an earlier review than a prior deferral; no old conversation creates authority.

## Quota ownership and actual recovery

The Quota keeper owns the periodic existing `backend_usage` collectors and
threshold-crossing notification receipts. It checks each provider window using
reported percent-used converted to percent-remaining, freshness and opaque
account/window identity. Unknown values do not imply exhausted quota. The
threshold is configured on the Janitor. APNs delivery uses the existing
notification transport; current desktop-presence and APNs configuration still
apply. Delivery receipts are retained. Retries reuse notification identity;
an ambiguous transport result is not proof of a unique phone delivery.

Recovery options:

- Notify only: no account change.
- Ask: create a durable native approval for the configured selector; only an
  accepted decision matching this Janitor generation can initiate recovery.
- Automatic: invoke the configured provider selector inside the owning runtime.

Configure absolute argv arrays in `[agents]`:

```
claude_account_switch_command = ["/absolute/verified/claude-selector"]
codex_account_switch_command = ["/absolute/verified/codex-selector"]
```

Selectors receive `{"models":[...]}` and must enforce the user's eligible
account/organization policy, activate a suitable saved identity and verify all
requested models before returning `{"available":true}`. No selector is installed
or credential state changed by enabling the keeper. An absent selector is
reported unconfigured; it is not silently treated as successful failover.
The configured selector command is trusted Host configuration, never supplied
by a model or web client. Provider collectors are not model inference calls.

Separate per-provider recovery coordinators fence callbacks, drain exact owned
process groups and resume original unfinished native conversations/model/effort.
User stop cancels ownership. Unknown tool outcomes must be reconciled by the
continuation before repetition. A newly spawned process must read the selector's
credential store; independent terminal sessions are outside this runtime scope.
Real credential isolation and selector deployment must be verified at rollout.
The change does not promise account-policy enforcement inside arbitrary scripts.

## Hotseat switcher

The Quota keeper reacts when a turn hits a limit. The Hotseat switcher acts
before that. It is not installed by default because it depends on the local
[Hotseat](https://github.com/Maxteabag/hotseat) CLI; create it from the Janitor
catalog (`clarp-admin janitor create --agent SESSION --template account-hotseat`
or the app's New Janitor screen). Enabled, it reads every saved account through
Hotseat (`hotseat list --json` for Claude, `hotseat codex --json` for Codex) and changes the machine-wide default
account while the one in use still has a little room. It is deterministic; no
model is called and no credential is read by the Host.

Options on the Janitor:

- `claude_min_remaining` (default 25): floor for the Claude 5-hour window, in
  percent remaining.
- `codex_min_remaining` (default 10): floor for the Codex weekly window.
- `interval_seconds` (300) and `codex_interval_seconds` (900): how often each
  provider is read. Codex readings probe one app-server per saved profile, so
  they are slower and rarer.
- `mode`: `automatic` switches through Hotseat; `notify` only reports what it
  would do.
- `hotseat_command`: the executable name or path (default `hotseat`, resolved
  on the Host service PATH).

The decision is a ladder. The first rung is the floor; below it come 10 % and
0 %. At each rung the account in use must be under the rung and another saved
account at or above it (and above the account in use); the account with the
most room wins, ties going to the larger weekly window. If nobody clears the
rung, the next rung is tried. A reading Hotseat could not make (`error`,
missing window) never counts as room, so a broken profile is never chosen. When
every account is out, one notification says so; it repeats only after the
situation changes.

Each provider check is a bounded demand run on the Janitor identity, so the
Janitors screen shows what was read and what happened. A switch, an advice in
notify mode and an exhausted state are delivered with the same receipts and
push transport as Quota keeper notifications.

Claude processes read the shared credential file on every request, so a Claude
switch reaches running turns. A running Codex session reads credentials once at
startup; after a Codex switch the Host retires idle Codex connections so the
next turn starts on the new account, and busy ones refresh at their next idle
admission.

## Global model chain

`GET/POST /janitor-policy` persists ordered `model_chain` entries and explicit
`inherit_sessions` using revision compare-and-swap. Each entry contains provider
and model. No fixed model-count cap; body/string validation still bounds input.
Native Janitor models offers add/remove/reorder, first entry as primary, and
per-Janitor inheritance toggles. Existing explicit settings remain unless opted in.
Provider compatibility is checked for inherited jobs. Managed primary providers
must match the Janitor backend. Deterministic/local workers cannot inherit models.

The same effective primary/fallback list is consumed by managed dispatch,
routing, tool explanations and heartbeat decisions. Global revision changes
invalidate demand output and fallback continuation. Existing fallback attempt
receipts and cancellation semantics remain authoritative. This does not migrate
account identity when selecting another model. Per-agent fallback editors no
longer impose an arbitrary four-entry cap.

## Review and rollout

Host tests use isolated SQLite, real HTTP adapters, real runtime orchestration
with fake process/model/notification boundaries. No real account switch, paid
model call, or APNs phone delivery is performed by these tests. Native editor
proof is separately recorded in the production handoff. Do not claim deployed
selectors or delivered push from a unit-test receipt. Aura coordinates registry
merges with the additive audio-bookkeeper role, then Host/runtime installation
and native release at the authorized integration boundary.
