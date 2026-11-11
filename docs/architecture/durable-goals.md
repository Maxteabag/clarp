# Durable task plans and continuation

## Behavior contract (written before implementation)

The existing task plan is the durable goal, with one immutable original outcome,
owner, explicit acceptance criteria and limits. Multiple commitments may coexist.
Creating a plan must not cancel another. Agent-selected steps may evolve with
revision checks and recorded reasons; required acceptance criteria cannot be
silently removed by skipping steps. Completion requires evidence for each
criterion and no unfinished required work. A final assistant message is not
completion.

Progress and execution are separate. Completed means accomplished; skipped,
deferred and removed work retain their reasons and history. Legacy records are
readable, retain their recorded status, and are explicitly not enrolled in
recovery. Ambiguous old completion is not upgraded into verified accomplishment.

A checkpoint atomically persists progress, evidence, next work and an optional
wake or external dependency. A dependency the goal still waits for is replaced
only explicitly: a checkpoint without a continuation keeps it, a timer over it
is refused, and a replacement is recorded. Continuation belongs to the goal and the bound
agent/native conversation. Revisions, generation fencing, dispatch idempotency
and expiring claims prevent stale edits or duplicate work. Scheduling and
admission are distinct from execution; failures and retries remain visible.

The existing Host scheduler reconciles only explicitly enrolled unfinished
goals. A live turn suppresses another wake. Early finals (including interviews
and status answers), lost workers and Host restarts remain recoverable. Paused,
cancelled or superseded goals, pending approval, unavailable capacity, missing
owners and changed native bindings must never launch work. Dependency deadlines
produce an honest recovery checkpoint rather than an invented job success.
Bounded retries eventually require attention. Explicit stop must suppress recovery.
Only work that can still run suppresses a wake: a queued row whose turn is over
and that nothing may launch again is not live work. A turn is over when it
settled or was closed. One that was opened and never closed counts as over only
when no spawn was recorded for it, a later turn of the same agent began at least
two minutes ago, and the runtime no longer holds the agent's slot for it; a
turn that runs or waits for an account holds its slot until it finishes. The
scheduler retires its own dead wake rows on every pass.

A wake the scheduler sent that did not complete is recorded once, as
`wake_undelivered` (it never started) or `wake_failed` (it started, then
failed), and the continuation's reason says which, why, and when the next try
is. Retries follow the backoff, which starts at the dispatch: 2, 4, 8, 16 and
32 minutes, then hourly.

- With the default recovery limit of 5 and wakes that fail at once, wakes go
  out at minute 0, 2, 6, 14 and 30 and the goal reaches `attention` at minute
  62. A try that first waits in the account park (up to 30 minutes) fails that
  much later, and its backoff has usually passed by then: five such tries go
  out at 0, 30, 60, 90 and 120 and reach `attention` at about minute 152.
- A wake that fails only because no account has usage left until its window
  resets does not count against the limit. It keeps its growing backoff and is
  tried until usage returns. The exemption is narrow on purpose. The error
  category `usage_limit` also holds depleted credits, payment (402), a
  suspended account, a monthly or hard billing limit, a trial quota and a model
  that refuses this client; no reset clears those. The split is made where the
  category is defined (`error_classify.usage_limit_resets`: a text that reads
  as a spent window and as nothing else of the category), and the provider's
  own statement overrides it where the Host records one (`backend_usage`:
  `credits_depleted`). Everything not known to clear at a reset counts, as
  `billing`.
- Sign-in, plan and billing failures (`auth`, `account_plan`, `billing`) count,
  because a person has to act; the `attention` reason says what: sign in, switch
  account or model, pay or raise the limit.
- The ceiling under the exemption: a goal none of whose wakes has started for 24
  hours (`task_goal_recovery.WAKE_START_CEILING_MS`) goes to `attention`
  whatever the reason, and the reason names the last failure. The clock starts
  at the dispatch of the first wake that got nowhere since one last started; a
  wake that was launched only to be turned away for lack of usage has not
  started in this sense. A wake that runs, a checkpoint or a resume clears it.
  It is checked when the next try is due, so with the hourly backoff it fires
  within an hour after the 24th.

What is still not announced:

- `attention` is a state in the goal. Nothing pushes a notification when a goal
  reaches it, and the owner cannot checkpoint because nothing wakes it; a
  person has to resume the goal.
- A dead queued row of another origin (a user or peer message) no longer holds
  goal wakes, but still holds heartbeat, janitor and dreaming wakes and is
  still listed as queued. Only goal wake rows are retired.
- A never-closed turn of an agent that has not run any turn since is not judged
  over. The runtime now closes such a turn when it abandons the launch, so
  this concerns only rows left by an older runtime.

Native cards and detail show completed/working/remaining/blocked/deferred counts,
current work or concrete wait, next wake, last checkpoint, evidence and revisions.
Pause/resume/cancel use the same authoritative Host goal. Old Hosts and old
artifacts remain readable during a rolling update.

## Required executable evidence

- Isolated SQLite reproduction of skip-only completion, pending completion and
  implicit replacement on the pinned baseline, then regression coverage.
- Actual persistent store and dispatcher integration: early final/status turn,
  missing worker, restart, dependency success/failure/timeout, blocked capacity,
  duplicate wake and concurrent mutation, stale/wrong owner, pause/cancel/approval.
- Migration preserves records and enrolls zero historical goals automatically.
- Native decoding and presentation tests for the same fixture states, Release
  compile and inspected isolated UI screenshot. All repository gates.
- Both remote main branches contain the resulting commits; deployment, upload
  and device installation are separate proof and outside this endpoint.

## Agency and adaptation

A durable goal commits to an outcome, not a frozen execution script. Agents may
add, split, reorder, replace or retire working steps after discoveries, select
fresh actions and write new self-prompts without routine approval. Meaningful
changes retain their reasons. Replaced/no-longer-needed work is never counted
as accomplished; retiring a method does not retire an acceptance criterion.
Completion may therefore resolve all original criteria with evidence while
obsolete methods remain visibly retired. Skipped or deferred required work
still needs resolution. The Host supplies ownership, boundaries and recovery,
not tactics, a fixed cadence, or a universal sequence of phases. A wake carries
the current revision/checkpoint and agent-authored intent as provisional context
and explicitly asks the agent to reassess the current situation. Replanning
supersedes older wakes. Verification includes a mid-goal discovery with revised
steps, changed prompt and a fenced-out old continuation.

## Goal-scoped working context

Named Markdown documents and JSON records attach to the same plan identity.
Their content shape belongs to the agent; only name, format, revision, reason
and timestamp are standardized. Every version is retained. A checkpoint can
write several documents, criteria evidence and a continuation in one transaction;
a stale plan or document revision aborts all changes. Wake context includes the
current document index, not a frozen replay of earlier notes. `clarp-goal read`
and the native document reader retrieve current or historical versions.

The database remains authoritative. Exports are explicitly versioned working
copies, never a second silently synchronized store. Each document is capped at
64 KiB; large logs/media remain managed evidence references. This persistence
survives process/restart boundaries, not loss of the Host disk. Replication,
distributed execution and account/credential failover are outside this feature.

## Rollout and proof boundaries

Host contract 21 advertises `durable_task_goals`; native controls are feature
gated and old plan payloads still decode. Schema 98 removes the one-active-plan
constraint and adds goal metadata, observed-change history and document versions.
Existing statuses and missing evidence are preserved, with `legacy=true` and
`completion_verified=false`. Recovery enrollment defaults to false, including
all migrated records. Opt in one current commitment at a time after verifying
its owner/native binding and acceptance criteria. Do not launch a fleet-wide
migration wake.

A provider-native goal remains provider-owned. The Host commitment never
silently turns on a second native goal or supersedes its provider objective.
While that loop owns continuation, the UI reports its ownership or blocked
state; Host wakes resume only after it no longer owns continuation. Generic
prompts remain available for independent reminders.

Goal pause/cancel fences future continuation and subsequent plan writes. It does
not kill arbitrary processes already running for an agent with several goals.
The native controls explain this distinction; the conversation's explicit Stop
interrupts the active turn and persists pauses for its durable commitments.

Deadlines are absolute UTC milliseconds in SQLite, not relative systemd timers.
Restarting the Host does not rearm the chosen delay. Completion and explicit
controls retire stale wake metadata as well as preventing admission. A pending
approval always blocks recovery; optional nonblocking questions do not prevent
otherwise authorized independent work. `completion_verified` means the recorded
criteria/evidence completion gate passed, not independent certification of an
agent's evidence or a deployment/device result.
