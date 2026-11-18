# Inputs to the keeper decision pass

The memo belongs to one autonomy worker and one keeper configuration/generation. A cold worker reads storage. A pass uses one deferred SQLite snapshot for its head tokens, plan point reads and recent-message metadata. The plain reader remains the oracle. The model prompt, receipt writer and update path continue to read authoritative data.

| Input to due and its decision helpers | Writer/authority | How a pass observes change |
|---|---|---|
| plan_id, agent_id, created_at | task_plans.create inserts these. They identify the owner, progress, time origin and helper lookback. | Live headers every pass. Enumerate in p.rowid order for the capped ready list. |
| status | task_plans.finish, _auto_finish, cancel_for_agent, _goal_mutate update status. Some completion paths need no revision increment. | Filter active/paused from current headers; use current status for catch-up and stall. |
| Agent archived_at, deleted_at, is_janitor; configured scope | Agent lifecycle and janitor configuration writers | Current join and scope filter on every pass, including additions/removals. A new generation gets a new memo. |
| revision, updated_at, newest goal_events.event_id | create, update_item, finish, _goal_mutate and pause_recovery_for_agent advance owner revision and/or timestamp. updater_apply advances timestamp without advancing owner revision. goal_ledger.record_goal_save appends new history inside the saving transaction. | Compare the entire token. Include keeper ledger entries for invalidation even though those entries do not themselves trigger keeper work. Equal timestamps still distinguish different ledger entries. |
| Nonempty goal_json membership | create/_goal_initialize and _goal_mutate enrollment/initialization. _goal_save serializes and records its history. | Full point load on initial observation or token miss, including cached negative membership. recovery_enabled alone does not establish membership. Every twentieth pass checks the original plain predicate and ordering. |
| checkpoint.at | Goal checkpoint/resume/enroll mutations in task_plans._goal_mutate (the new checkpoint replaces the previous one) | Owner revision/updated_at and appended history. Use created_at if absent. The cutoff based on now is recomputed per pass. |
| updater presence, through_ms, through_event | task_plans.updater_apply writes the full updater projection plus an updater history entry, persisted as a goal event with delegation_id goal-keeper. | Timestamp and event ID even when owner revision stays the same. Preserve updater presence separately from whether through_ms is truthy. |
| updater.progress | updater_apply as above | Used for the existing stall line only in the sweep branch. Read afresh on every sweep. |
| continuation.state and due_at | _goal_initialize, owner _goal_mutate, pause_recovery_for_agent and recovery CAS (task_goal_recovery through task_goal_state.save_if_unchanged). Recovery can change observation/wait metadata without an owner revision; a save with no new history need not advance the event token. | Used by stall() on the sweep decision path. Always reload storage on a sweep. A simultaneous sweep and twentieth-pass comparison use the same plain rows. |
| Other plan columns and goal keys | Documents, subgoals, ledger, history, receipts and other goal mutations can advance a token without affecting due(). | No decision reads their contents. A token change may cause a conservative reload. The projection guard in test_goal_keeper_projection.py checks decision-time accesses. |

Helpers are selected each time using the original parent, deletion, created_at, ordering and limit. Retry/catch-up state, next sweep, current clock, first trigger time, fold window and watch switches belong to the caller's current progress/config. Neither a future deadline nor a ready verdict is cached. Sweep stall MAX, terminal jobs, owner state and goal events remain ordinary reads.

Message memo hits require the same agent, exact requested cursor and complete ordered session head vector in the same database. The covering revision index is used only to seek across session names to detect headless sessions. No message index or writer change is required. One retained entry per currently visited agent per configuration contains message presence, newest update time, final timestamps and user-message senders/times; entries exceeding the retention cap are read again. A new helper in a plan, missing session head, changed or decreased revision/replace revision, different cursor, catch-up or cold restart performs the ordinary message query. Role labels and watch switches are applied for the current goal. A carry publishes the destination head only; the whole-agent vector changes. An AGY restore publishes a replacement head even when the restored rows have older revisions.

Every twentieth pass checks the authoritative plan list and the plain batched message query in the same snapshot. The receiver compares raw facts and Activity, including sorted triggers with multiplicity, and uses authoritative results for that pass. One content-free mismatch event identifies the number of affected goals and field classes and the memo is discarded. On exception, partial progress is discarded before the plain pass.

Both readers return open plans in ascending task-plan rowid order (oldest inserted first). The keeper takes its capped batch from that order. Both installed Hosts returned this order before the change and had empty SQLite statistics. A database on which the planner previously chose another order after ANALYZE can therefore get a different batch.

One existing writer can change a row under another agent when a caller supplies a colliding message ID. Only the caller's head advances. The affected agent's message memo can then serve stale facts until the plain comparison, at most twenty passes. The review fixture observed 18 stale answers before correction. The writer repair is tracked separately.

## Read diagnostic

Set `[diagnostics] janitor_autonomy_reads = true` to measure intervals of 20 autonomy passes. The Host writes the interval to stderr (the service journal) whenever this switch is on. The ordinary capture policy controls whether the same fields also persist in telemetry. Turning the switch off discards a partial interval. The default path does not open procfs or install a SQL wrapper. One `janitorAutonomyReads` record reports decimal MB of thread rchar for setup, heartbeat, quota, hotseat, labels and keeper. For the keeper it also reports plan headers/points/plain rows, session-head validation, helper membership, messages, stall MAX, jobs, goal events, owner state and other queries. The keeper remainder includes eligibility/progress reads and operations outside the decision SELECTs. Lane totals already include query totals. A worked flag means a lane began a check/run, delivered a notification or applied setup, even if it found nothing to act on. Sweep and self-check flags mean at least one occurred in the interval. On a system without thread rchar the record marks sampling unavailable. Cached/mapped SQLite work need not register read bytes; use a warm live sample for the operator's target.

## Synthetic equivalence sample

Two independent scratch databases started with 23 goals and 43 agents, two sessions per agent, 128 historical messages per session. Over 26 fixed-clock passes the original reader and the memo reader produced identical ordered ready IDs, raw Activity fields/trigger sequences and entire progress maps. Inputs included same-millisecond activity, a terminal job then re-registration, retry/clock expiry, forced sweeps, scope changes, helper reassignment, closure, one-off catch-up and a cold worker restart. The real-session carry and AGY restore tests cover invalidation through production writers.

After warm-up, a stable idle agent/cursor hit. Five of the 23 goals had no updater cursor and a moving lookback; those five owners and two helpers missed on every such pass. Across the 43 agents that was 36 hits / 7 misses (83.7%). Changing one of the stable owners’ heads on every busy pass gave 35 hits / 8 misses (81.4%). The complete 43-agent, 86-session head lookup **including headless-session certification** took 5,932 SQLite VM operations, 1.50 ms and zero additional warm thread read bytes on this small mapped fixture. These numbers are shape-specific. No production thread throughput is inferred.


With memory mapping disabled, the review fixture measured these read volumes per pass in decimal MB. About 3.2 MB of the ordinary memo pass came from plan header pages.

| Pass | Plain | Memo |
| --- | ---: | ---: |
| Ordinary | 6.77 | 3.69 |
| Sweep | 6.78 | 7.03 |
| Twentieth-pass comparison | 6.79 | 10.26 |

The operator measures the live thread separately with the diagnostic switch.
