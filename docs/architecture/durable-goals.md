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
