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

A delegate's observation, claim, discrepancy or unknown also stores `new.sources`:
for each reference, what it pointed at when recorded.

| reference | status | kept |
|---|---|---|
| `message:<id>@<revision>`, that revision still current | `exact` | hash and first 300 characters |
| `message:<id>@<revision>`, row rewritten since | `changed` | only the current revision and hash, labelled current; the cited text is not retained |
| `message:<id>` without a revision | `unpinned` | current revision and hash |
| a revision newer than the row's | `invalid` | current revision |
| no such message of the principal, or one the delegate sent | `unavailable` | nothing |
| `goal_event:<id>` of the principal's goals, `document:<name>@<revision>` | `exact` or `unavailable` | immutable rows |
| any other prefix (`job:` …) | `unverified` | nothing |

A message row is rewritten not only while streaming but also when its turn
settles (metadata only, same text), so `changed` means "rewritten since", not
"the text changed". Excerpts are kept in the append-only ledger: text a user later
deletes from the conversation stays in the excerpt.

Existing `goal_json.history` is unchanged and still written; on upgrade each
historical entry is copied into `goal_events` with `actor_kind = legacy`.

`goal_subgoals` holds explicit subgoals: `title`, `intent` (why it matters),
`criteria`, `owner`, `status` (`proposed`, `active`, `blocked`, `done`, `retired`,
`unknown`), `evidence`, `current_action`, `next_dependency`, `last_observed_at`,
`revision`. A subgoal is never deleted; retiring keeps it with its reason. Every
change carries the subgoal's expected revision and appends an event with the prior
and new state, so concurrent edits conflict instead of overwriting.

`goal_delegations` records one delegate for one principal: status
(`active`/`stopped`), the hash of its credential (one of two checks, with the delegate's turn identity), the applied cursors
(`message_through`, `goal_event_through`), the dispatched cursors
(`dispatched_message_through`, `dispatched_goal_event_through`) and listener health
(`last_source_at`, `last_wake_id`, `last_wake_at`, `last_applied_at`,
`last_heartbeat_at`, `last_error`, `unapplied_since`). A cursor is capped at what
exists, so a bad value cannot silence the listener. A delegation starts at the
principal's last 200 messages (`baseline_messages`) and every goal event. Deleting
or archiving either agent stops it.

Each plan's JSON carries `subgoals` and a `ledger` envelope: `event_count`,
`last_event_at`, the active `delegation` with its health, and `accounting`, the
delegate's current books (its latest observation, claim, discrepancy or unknown
for each subject, newest first, at most 50). Earlier entries stay in
`GET /goal-ledger`.

## Who may do what

| actor | may | may not |
|---|---|---|
| owner (existing `clarp-goal` path) | everything it could before; add, edit and retire subgoals | — |
| delegate (credential) | read the principal's goals and conversation since its cursor; append observations, claims, discrepancies and unknowns with source refs; propose subgoals; update a subgoal's bookkeeping fields (`current_action`, `next_dependency`, `last_observed_at`, accumulating evidence, status `unknown` or `blocked` unless retired or done) | change outcome, criteria or limits; record criterion evidence; complete, pause, resume, cancel, block or supersede; approve; touch continuation or jobs; write any other agent's goals; message its principal or reply into its goal |
| system (Host recovery) | as today | — |

Identity: every per-turn provider process (Claude, AGY, Grok, OpenCode, Codex
exec) gets a fresh Host-issued token in `CLARP_PROVIDER_TURN`, recorded in
`provider_turns` with its agent (`lib.turn_identity`). Clarp tools inside the
turn, and workers it launches, inherit it. The goal store decides who is acting
from that token, not from a session name the caller types:

- A goal change through the goal CLI is recorded as `owner` when the token is the
  owner's, as `peer` under the caller's own id when it is another agent's, and as
  `owner` with `new.actor_verified: false` when there is no token.
- While a principal has an active delegation, every meaningful change to its goals
  (checkpoint, step, replan, pause, complete, create, subgoal edit, a peer-request
  wait or reply) is refused unless it comes from the user (the apps), the Host, or a
  caller whose token proves it is neither the delegate nor an agent the delegate
  started. This covers plans without a goal too. The owner path needs a token; a
  peer reply without one is kept, marked unverified, only when the replier runs
  on a backend that cannot carry a token (the shared Codex app-server). A caller
  whose token names someone other than the name it typed is recorded as itself.
- The Host refuses to create or fork an agent when the creator (the turn identity
  `clarp-admin` sends as `X-Clarp-Turn`), the named parent or the fork source is
  an active delegate or an agent it started, before anything is written.
- `bookkeeping observe` and `record` need both the delegation credential and the
  delegate's own token.
- A delegation needs both agents on a backend whose turns carry tokens, and stops
  if either moves off one.

Limits, stated rather than hidden: this is not cryptographic isolation and not
an OS sandbox. Agents share one Unix user and the Host API token, so a process that deliberately reads another process's environment, writes
the database directly, or calls the HTTP API as the user is not stopped by this.
The Codex app-server serves all Codex agents from one process and has no per-turn
token, so an unverified reply claiming a Codex replier cannot be told apart. A
token stays valid after its turn exits (workers it launched need it) until
`provider_turns` retention removes it after 7 days; a worker running longer, or a
turn whose token failed to record, is refused while a delegation is active; a
refused `clarp-admin reply` falls back to a plain message, so the answer reaches
the principal's chat but its goal keeps waiting until the deadline. An agent
creation request that carries no turn identity is treated like the user's, the
same HTTP limit as above.

## Listener

One listener per active delegation runs as its own systemd user service
(`clarp-bookkeeping-<delegation>`), outside the runtime's cgroup, registered as a
background job of the delegate. It polls the principal's message rows by message
revision and the principal's goal events by `event_id`, ignoring the delegate's own
events and any message the delegate sent. It coalesces a burst until it has been
quiet for a few seconds (bounded by a maximum delay), then wakes the delegate once
with a stable id (`bookkeeping-<delegation>-m<message revision>-e<goal event>`),
queued behind any running turn. It does not dispatch again until the delegate has
applied that wake, and never while an earlier wake for the delegation is still
queued or parked or the delegate's newest turn is unsettled (started within the
last 2 hours; an older unsettled turn is treated as stale): that is read from the queue and turn
records, not only the stored cursors, so a wake admitted just before a listener
crash (cursor not saved) or still running in a long turn is not joined by a
second one. A wake that ended without being applied for 15 minutes is resent under
`…-r<seconds>`, unless the previous one is still queued. A failed send backs off.
A listener job that fails (a missed heartbeat during suspend) is re-registered;
only cancelling the job stops the pilot. Cursors live in the database, so a
restart backfills from the last applied point. Stop: the delegation's status goes
to `stopped`, the listener exits and the job closes; no further wakes are sent.

Health (`GET /goal-ledger/delegations`) reports `last_source_at` (when the newest
unbooked activity happened: the message's own `timestamp` or the goal event's
`at`, never when a listener noticed it or a row was rewritten, so a restart does
not make old history fresh), `unapplied_since` (when the oldest unbooked activity
happened, not earlier than the delegation, recomputed on every apply), the last
wake and apply, `lag_ms` (how long that oldest activity has waited), `in_flight`,
the last heartbeat, and `last_error` (also why it is waiting). It does not promise
zero delay. Without a user systemd manager the listener cannot be checked
or restarted by the Host.
