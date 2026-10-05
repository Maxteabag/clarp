# Goal ledger and bookkeeping delegates

A durable goal (see [durable-goals.md](durable-goals.md)) is the primary record of
an outcome. The ledger makes every meaningful change to it attributable and
append-only. A bookkeeping delegate is one helper agent that observes its
principal's conversation and keeps the books on the principal's goals, without
being able to direct or change the work.

## What is stored

`goal_events` is append-only (SQLite triggers abort UPDATE and DELETE). One row per
meaningful change:

| column | meaning |
|---|---|
| `event_id` | global, monotonic cursor |
| `plan_id`, `subject` | the goal, and `goal`, `criterion:<id>`, `step:<id>` or `subgoal:<id>` |
| `kind` | what happened (`checkpoint`, `replan`, `subgoal_added`, `observation`, `claim`, `discrepancy`, …) |
| `actor_kind` | `owner` (the goal CLI), `user` (the apps), `delegate`, `peer` (a reply to a request), `system` (Host recovery, agent deletion), `legacy` (copied on upgrade) or `unattributed` (a path that names no actor) |
| `actor_agent_id`, `delegation_id` | who, when known |
| `basis` | `observed`, `claimed`, `inferred` or empty: separates owner claims from facts seen |
| `source_refs` | JSON list of exact references (`message:<id>@<revision>`, `job:<handle>`, `goal_event:<id>`, `document:<name>@<rev>`) |
| `reason`, `prior_json`, `new_json` | why, and the state before and after |
| `plan_revision` | plan revision the change was made against |
| `idempotency_key` | unique per plan; a replayed write is a no-op |
| `at` | Host time in ms |

Existing `goal_json.history` is unchanged and still written; on upgrade each
historical entry is copied into `goal_events` with `actor_kind = legacy`.

`goal_subgoals` holds explicit subgoals: `title`, `intent` (why it matters),
`criteria`, `owner`, `status` (`proposed`, `active`, `blocked`, `done`, `retired`,
`unknown`), `evidence`, `current_action`, `next_dependency`, `last_observed_at`,
`revision`. A subgoal is never deleted; retiring keeps it with its reason. Every
change carries the subgoal's expected revision and appends an event with the prior
and new state, so concurrent edits conflict instead of overwriting.

`goal_delegations` records one delegate for one principal: status
(`active`/`stopped`), the hash of its credential, the applied cursors
(`message_through`, `goal_event_through`), the dispatched cursors
(`dispatched_message_through`, `dispatched_goal_event_through`) and listener health
(`last_source_at`, `last_wake_id`, `last_wake_at`, `last_applied_at`,
`last_heartbeat_at`, `last_error`, `unapplied_since`). A cursor is capped at what
exists, so a bad value cannot silence the listener. A delegation starts at the
principal's last 200 messages (`baseline_messages`) and every goal event. Deleting
or archiving either agent stops it.

## Who may do what

| actor | may | may not |
|---|---|---|
| owner (existing `clarp-goal` path) | everything it could before; add, edit and retire subgoals | — |
| delegate (credential) | read the principal's goals and conversation since its cursor; append observations, claims, discrepancies and unknowns with source refs; propose subgoals; update a subgoal's bookkeeping fields (`current_action`, `next_dependency`, `last_observed_at`, accumulating evidence, status `unknown` or `blocked` unless retired or done) | change outcome, criteria or limits; record criterion evidence; complete, pause, resume, cancel, block or supersede; approve; touch continuation or jobs; write any other agent's goals; message its principal or reply into its goal |
| system (Host recovery) | as today | — |

Identity: agents on one Host share a Unix user and the Host API token, and the
Codex app-server is shared, so no caller identity is cryptographic. The delegate
credential is a per-delegation secret in a 0600 file; its value is that only the
delegated path is told to use it and that what it can do is narrow and recorded.
Owner-path writes are recorded as `owner` because that path writes as the owner;
it does not prove which process called it. The goal CLI refuses owner commands
from a Claude caller that is its principal's delegate; it does not check Codex
callers, because the shared app-server's session variable can name another agent
and would lock a real owner out.

## Listener

One listener per active delegation runs as its own systemd user service
(`clarp-bookkeeping-<delegation>`), outside the runtime's cgroup, registered as a
background job of the delegate. It polls the principal's message rows by message
revision and the principal's goal events by `event_id`, ignoring the delegate's own
events and any message the delegate sent. It coalesces a burst until it has been
quiet for a few seconds (bounded by a maximum delay), then wakes the delegate once
with a stable id (`bookkeeping-<delegation>-m<message revision>-e<goal event>`),
queued behind any running turn. It does not dispatch again until the delegate has
applied that wake. A wake left unapplied for 15 minutes is resent under
`…-r<seconds>`, unless the previous one is still queued. A failed send backs off.
A listener job that fails (a missed heartbeat during suspend) is re-registered;
only cancelling the job stops the pilot. Cursors live in the database, so a
restart backfills from the last applied point. Stop: the delegation's status goes
to `stopped`, the listener exits and the job closes; no further wakes are sent.

Health (`GET /goal-ledger/delegations`) reports when the newest activity was first
seen, the last wake and apply, `lag_ms` (how long the oldest activity not yet in
the books has waited), `in_flight`, the last heartbeat and error; it does not
promise zero delay. Without a user systemd manager the listener cannot be checked
or restarted by the Host.
