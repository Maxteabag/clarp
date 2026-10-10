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
   Prefer the normal idle release handoff. A manual runtime restart requires
   authorization because it interrupts in-flight work.
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
