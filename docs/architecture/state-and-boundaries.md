# State, boundaries and abstractions

Status: accepted 2026-09-26. Owner: Peter. This is the contract for the
`refactor/architecture-program` change and for code written after it. It
follows the backend strategy contract (`backend-strategy.md`): same tone, same
rule that the host never spells what an owner already knows.

## The rules

1. **One owner per fact.** A fact that must survive a restart lives in SQLite
   and is written by exactly one store module. Memory holds views, never the
   truth. Where memory must be fast (turn slots, caches) it is a written-through
   view with an explicit invalidation key that works across processes.
2. **Module state is constructed, not global.** Mutable state belongs to an
   object built in a composition root (`build_server`, `runtime.py`) and passed
   in. A module-level container is allowed only for process-wide singletons
   (registries, locks) and must be bounded, guarded and registered for test
   reset. A guard test bans new ones.
3. **Identity is typed.** An agent is an `AgentRef`; a turn is a `TurnRef`.
   Strings are parsed into them once at the boundary. Translating between
   `session`, `agent_id`, `backend_session_id` and `trace_id` happens in
   `identity.py`, nowhere else.
4. **Transitions go through the state machine.** `state_log` and `turns` are
   written only by `turn_lifecycle`. Every writer calls `transition(...)` with
   an event; illegal transitions are refused, logged and counted.
5. **Stores own their tables.** `settings`, `agents`, `messages`,
   `janitor_*`, `artifacts` and the rest are written only from their store
   module. Other modules call the store's public functions. Raw SQL against a
   table you do not own is a review failure; a test counts writers per table.
6. **Policies are pure.** Which origin may wake an agent, whether a completed
   turn notifies, whether to fail over, how to parse an agent spec: functions
   from values to decisions, with no IO, tested as tables. The IO code calls
   them and acts on the decision.
7. **Events are typed.** Every SSE event has a constructor in `events.py`
   returning the dict the wire expects; `broadcast()` accepts only those. A
   schema test cross-checks `protocol.md`.
8. **The handler is the only HTTP surface.** Library modules never read
   `handler._request_*` or write to `wfile` directly; they take a `Principal`
   and a `Responder` from `http_utils`.

## Layout

```
server/lib/identity.py        AgentRef, TurnRef, resolve(), trace_id()
server/lib/turn_lifecycle.py  TurnStateMachine: transitions, busy/terminal sets,
                              the only writer of state_log and turns
server/lib/turn_slots.py      TurnSlots: in-flight, queued, claimed; durable
                              through the turns/queued_turns rows
server/lib/events.py          one constructor per SSEType; broadcast() type-checks
server/lib/revisioned_cache.py  bounded, locked cache keyed by a cross-process
                              revision (PRAGMA data_version or a table revision)
server/lib/policies/          admission.py, notifications.py,
                              failover.py, agent_spec.py: pure decisions
server/lib/janitor_store.py   the janitor tables' only writer
server/lib/http_utils.py      Principal, Responder, require_full_scope()
server/lib/context.py         frozen ServerContext with typed services
```

## Workstreams

Four streams run in parallel on separate branches from this commit, each
owning the files below and nothing else. Integration merges them here.

| Stream | Owns | Delivers rules |
|---|---|---|
| A turn | turn_lifecycle.py (new), turn_slots.py (new), turn_dispatch.py, turn_queue.py, reconcile.py, state_watcher.py, agents.py (state/turn functions only), plugin/hooks/*, runtime_bridge.py dispatch RPC | 1, 4, plus DispatchCommand serialized once across the RPC |
| B identity+events | identity.py (new), events.py (new), trace.py, protocol.py, herald.py, orchestrator.py, backend_usage.py, message_audio.py, server.py (broadcast and identity sites only) | 3, 7 |
| C state+stores | revisioned_cache.py (new), db.py (data_version), agent_conversations.py, message_previews.py, settings_store.py, janitor_store.py (new), janitors.py, janitor_builtins.py, agent_portraits.py, application_activity.py, desktop_presence.py, oracle_contact.py, http_utils.py (Principal/Responder), the 7 oracle/podcast/janitor_http modules that read handler privates, context.py, config.py, tests/conftest.py resets, the module-state guard test | 1, 2, 5, 8 |
| D policies | policies/ (new), user_notifications.py, account_failover.py, agent_lifecycle.py, origins.py | 6 |

Cross-stream adoption (dispatch calling the admission policy, turn_dispatch
using event constructors, stores using AgentRef) is done at integration, not
inside a stream.

## Integration status

Integrated on this branch 2026-09-26. Every rule has a guard test: rule 2
`test_module_state_guard.py`, rule 3 `test_identity.py`, rule 4
`test_turn_lifecycle.py`, rule 5 `test_table_writers.py`, rule 6
`test_policies_pure.py`, rule 7 `test_events_schema.py` plus the hub check in
`test_audio_stream.py`, rule 8 `test_http_utils.py`.

What is still open, and why:

- Backend turn callbacks (`on_init`, `on_result`, `on_error`) run whole
  under `_TURN_LOCK`, so the state rows they write are written under it.
  Moving them out means splitting each callback into a decision taken under
  the lock and effects run after it. That is a change to the turn loop, not
  a move of code.
- `snapshot.py` reads the compaction set once per snapshot from the runtime
  status instead of asking `live_work()` per agent. This is deliberate: one
  socket round trip per snapshot.
- The streaming routes (`audio_growing`, `clip_stream`, `terminal_ws`) still
  write to `handler.wfile`, because `Responder` has no streaming API yet.
- `AgentState.busy_states()` in `protocol.py` keeps its own set. The
  protocol module is a leaf and cannot import `turn_lifecycle`; a test
  asserts the two sets are equal.
- `trace.LEGACY_TRACE_ID_RE` stays for rows stored before this change.
  Nothing mints those shapes any more.
