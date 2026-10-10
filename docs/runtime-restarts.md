# Runtime restarts and owned handoff

A runtime restart performs state recovery without asking an assistant to run a
generic continuity check. The runtime preserves native conversation bindings,
marks interrupted turns and their user messages visibly, reconciles state, and
recovers valid explicitly queued requests. HTTP-only restarts leave a healthy
runtime's work alone. Existing explicitly enrolled goal timers are independent
commitments, not blanket restart checks.

The agent responsible for a planned runtime restart owns continuation:

1. Before restarting, read the authenticated `/agents/snapshot`, runtime status,
   current native bindings and queued requests. Save a private inventory of the
   exact agents and native threads doing unfinished authorized work. Inspect
   their latest substantive output and current goal/checkpoint; a persisted
   runtime, `alive`, old task or stale busy label is not evidence of work.
2. Preserve current approvals, user pauses, capacity waits, active helpers and
   queued continuation ownership. Do not include finished/idle conversations,
   diagnostic probes or routine heartbeat checks in the restart target list.
   Prefer the normal release handoff (the graceful drain below). A manual
   runtime restart requires authorization because it interrupts in-flight work.
3. After restart, verify the running runtime's release and authenticated Host
   health. Re-read each intended target immediately before intervening. If it
   has already resumed, has a valid continuation queued, is legitimately waiting,
   or now reports completion, do not send another prompt. Keep the same native
   thread and task constraints.
4. Directly message only genuinely stranded targets from the restarting agent,
   as itself: `clarp-admin prompt --to EXACT_SESSION --from YOUR_OWN_SESSION --text TEXT`.
   The Host refuses a `--from` that is not the calling turn's own agent.
   Compose a task-specific instruction naming the original request/checkpoint,
   operations whose results need reconciliation, acceptance criteria and limits.
   A generic instruction to search historical chats is not a handoff.
5. Verify an actual new assistant message or tool call in the target's native
   thread. An admitted message, process launch or `busy` badge is not enough.
   Report exactly who resumed and who remains waiting, rather than prompting
   the fleet again.

If the restarting agent itself will be interrupted, persist this inventory and
verification instructions in its existing goal checkpoint and arrange a scoped
owned continuation before restarting. Do not create a fleet-wide replacement
supervisor or depend on runtime startup to infer the work.

An unexpected crash has no agent authorizing a fresh inference request. Its
interruption markers remain visible until the user, an explicitly enrolled goal,
or an authorized recovering agent decides what to continue. Startup never spends
provider quota to ask idle conversations whether they have anything to do.

## Graceful release drain

`RuntimeReleaseMonitor` (`server/lib/runtime_release.py`) hands a runtime over
to a newly installed release without interrupting anything:

1. It targets the release in `share/current` only when that release has both
   `RUNTIME_READY` and `INSTALL_OK` (the installer writes the latter after the
   health check), so a release still installing or being rolled back is never
   a target. A fully idle runtime hands over at once, as before.
2. Otherwise it raises the admission fence (`TurnDispatchService.begin_admission_fence`):
   in-flight turns, their retries and account-recovery resumes finish
   normally; no new turn starts. Every arrival (user, peer, automation,
   recovery, explicit send-now) is admitted to the durable queue in order and
   held; a normal send that would have preempted or steered waits instead.
   Held work without a queue row gets a `drain-park-<trace>` park row, rows are
   reordered to the agent's arrival order, and work a Stop pause could not hold
   on arrival keeps `queued_turns.allow_paused` so the next runtime honours it.
   Pauses, cancels and Stop keep their meaning.
3. It seals (today's hard fence, `draining`, dispatch RPCs answer 503 for the
   moment before exit) only when nothing is owned: no active or spawning slot,
   terminal, compaction, Stop lease, account recovery park
   (`claude/codex_account_recovery`), shared-provider turn (a Codex app-server
   goal continuation) and no held work that is not yet durable. Then
   `mark_clean_handoff`, shutdown, and the new runtime recovers the durable
   queue exactly as at every boot.
4. The wait is monotonic and bounded: 15 minutes from raising the fence (a
   newer release retargets the same drain without extending it). When it
   expires the fence is lifted, held work starts in order with nothing lost or
   repeated, and the next attempt waits 5, 10, 20, 40, then 60 minutes. A
   rollback or lost readiness aborts the drain and forgets the backoff.

Runtime status (and `runtime.drain` in `GET /status`, docs/protocol.md; no
contract bump, diagnostic only) shows `drain` (phase, target and running release, attempt,
`fenced_since`, `deadline`, `next_attempt_at`, `blockers` by kind,
`last_outcome`) and `held` (agents whose next turn waits on the fence). An idle
agent with held work shows the live headline "Waiting for the Clarp update".
The fence lives in process memory: a runtime that crashes mid-drain restarts
unfenced, recovers the durable queue, and its monitor decides again.

The budget comes from the live Host's history (7 days of `turns`/`state_log`
to 2026-10-10, turn end = first terminal state): single turns p50 28 s, p90
10 min, p95 25 min, p99 84 min, with some turns running 4 to 6 hours. Raising
the fence at each minute of the week, every in-flight turn had finished within
15 minutes 60% of the time (p50 6 min, 30 min 70%, 60 min 82%). Replaying the
policy, 15 minutes with a 5 to 60 minute backoff handed over within 1 hour 78%
and within 6 hours 96% of the time, with a mean 25 minutes of held admissions
per handoff; 30 minutes only reached 81% within 1 hour while doubling the
longest wait any one message sees.
