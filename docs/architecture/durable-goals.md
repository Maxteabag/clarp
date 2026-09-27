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
wake or external dependency. Continuation belongs to the goal and the bound
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
