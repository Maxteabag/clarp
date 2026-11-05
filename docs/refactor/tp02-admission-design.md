# TP-02: replace whole-RPC serialization with explicit admission ownership

Design for lead 631 and reviewer A2 rf-review-core2c-d509. Implementation is
AFTER big merge #1, outside the Friday/Saturday releases and outside big
merge #1 itself. This document changes no code and is not implementation
approval or risk acceptance.

Source inspected: main 7e3b14592db832b77b4db4e004b4def2ebc8a6b6 and canary
a89dfc836995678ecd660fd94843134edb90af1b. The inspected runtime_bridge.py,
turn_dispatch.py, turn_slots.py and server.py have no difference between
those pins. Integration a7331017 still has the RPC lock; S4 changes where
turn methods live, not this protection. Before implementation, rebase this
inventory on the then-landed Friday/Saturday/Sunday fixes, especially
BACKENDS-07b and TP-N3. Line references below are to the inspected main.

## Outcome and deliberately retained boundaries

A provider start or other slow effect for agent A must not own admission
for B, status, or A's Stop. Preserve durable admission/idempotency,
per-agent queue order, exact-owner preemption, Stop pause/receipt/barrier
semantics, and drain's proof that no accepted work can be lost at handoff.
Removing one lock without replacing its implicit drain protection is
incorrect.

Keep protocol version, request/response shapes, HTTP/CLI output, live
conversation identity, database format and paths unchanged. SQLite still
serializes writers; a shared provider/account selector may have independent
contention. Removing the RPC mutex is not a promise that these waits
disappear. No lifecycle SQL change or TP-N4 code is part of this design.

## What the current lock actually covers

RuntimeRPCServer.dispatch_request holds _admission_lock across both the
hard-draining check and ALL of _dispatch_request
(runtime_bridge.py:518-525). ThreadingMixIn creates concurrent handlers,
but a handler waits here before any method body. Decode and response writes
are outside the lock; live_stream bypasses dispatch_request entirely
(:381-408). Native callbacks, local dispatches, queued handoffs, account
recovery and Stop-lease timers are not universally serialized by this
mutex.

| Current entry | Work protected by the outer RPC mutex today | Required narrower owner |
| --- | --- | --- |
| dispatch | Resolve target; validate; bind/check session; commit admission/receipt; claim, queue, steer/preempt; invoke backend; post-success bookkeeping | One canonical-agent admission lane, then an owned effect ticket |
| dispatch_queued | Read/claim queue row and submit its actual agent | Queue-row transaction plus that canonical-agent lane |
| recover_queued | Entire multi-agent scan and its admissions | Scan coordinator plus individual per-agent admissions |
| begin_stop | Install Stop barrier; user receipt fence; interrupt; rollback or publish lease | Agent control transition, captured interrupt target and lease ownership |
| finish_stop | Remove lease and restore/filter/drain queue | One completion claimant retained through effects |
| interrupt / interrupt_any / steer | Capability check and provider operation | Agent/turn-specific effect claim; preserve receipt-before-steer |
| release_agent | Clear slot, interrupt, soft-delete | Agent lifecycle lane with terminal generation |
| release_leaked_slots / repair_slot | Check and repair that agent's ownership | Agent repair lane and existing exact-owner checks |
| goal | Lookup plus the complete native goal RPC | Agent/native-conversation effect claim; backend keeps its own ordering |
| compact | Lookup, busy check, publish compaction and start its worker | Session/agent admission plus existing compaction ownership |
| cancel_janitor_run | Find and fence exact run, barrier and cancellation | Exact run/agent control claim |
| recover_janitor_quota | Provider/owner recovery, potentially scheduling work | Provider/owner recovery claim; each resumed agent re-enters admission |
| explanation_command | Runtime command lookup, potentially native reader work | Agent-specific operation, existing command/cache locks |
| status / live_snapshot | Status/provider/hub observation | Read snapshots under their actual short state locks |
| live_observe / live_patch | Live projection writes | Existing hub sequencing/locks; no launch-admission ownership |
| unknown method | Existing 404 response | Validation only |

The hard-draining rejection set is ONLY dispatch, dispatch_queued,
recover_queued and steer (:520-524). Do not claim that the current lock
forbids every post-seal mutation: compact, goal and quota recovery are not
in that set. See the explicit release-policy decision below.

The same RLock protects begin_drain_if_idle (:796), soft fence raise/lower
(:821/825), seal_now (:880), seal_if_drained (:917), and cancel_handover
(:960). It prevents an RPC from sitting invisibly between acceptance and
its first slot while a seal checks idleness. It also prevents a seal from
crossing RPC finish_stop after that method removes its dictionary entry.
These protections must be made explicit.

Stop leases have a SEPARATE mutex (:490/661/682). Timer expiry
(:989-1005) pops under that mutex, releases it, and calls complete_stop
WITHOUT _admission_lock. Existing code already has a lease-removal-to-
completion gap on this timer path; the RPC lock is not its protection.
Backend callbacks and start_held_work also run outside the RPC lock.
TurnSlots' short OwnershipLock owns claims/queues, trace-conditional
release and Stop sentinels (turn_slots.py:309-337,451-515).
_admitting_send is an observation/reconcile marker, not a replacement
admission lock (turn_dispatch.py:300,1496; turn_slots.py:208-231).

### Host-side queue edits and Stop

The Host obtains begin_stop(hold=True), retains its opaque lease while
pausing goals/writing STOP_REQUESTED and queue events, and finally sends
finish_stop; deferred Oracle cancellation can edit/remove work before
that finish (server.py:5210-5350). The lease/barrier spans that interval;
the RPC mutex does NOT. Queue edit/reorder/remove/resume use turn_queue
transactions/revisions in the Host, with runtime recovery/manual dispatch
as separate calls (server.py:5800-5868). Do not relocate those writes or
claim they are covered by a runtime per-agent mutex.

The existing barrier retains the captured queue, merges later arrivals,
filters cancelled trace IDs, preserves durable paused entries, and starts
eligible work only at completion. A failed Stop restores its snapshot and
pause and hands newly admitted work onward; strict failure remains failure.
Preserve the distinction between USER Stop (user-stop generation and
pending receipt fence) and maintenance/verified-agent cancellation.

## Proposed admission model

Use a coordinator for short metadata transitions, operation tickets that
remain visible through slow effects, and per-agent ordered admission
lanes. A per-agent mutex around the WHOLE current RPC would still block
Stop behind its own provider start and is rejected.

### 1. Reserve before any invisible admitted interval

At entry to every mutating operation, under the short coordinator mutex:
check release phase, register a unique process-local ticket, and advance
an operation generation. This happens BEFORE target lookup or any DB,
filesystem/provider operation. A ticket may initially have no agent.
Malformed/unknown/read-only calls can be classified without admitting
work; classification itself performs no mutation.

The ticket owns the operation until it (a) fails with no side effects,
(b) transfers responsibility to an existing slot, durable queue receipt,
compaction/recovery owner or Stop lease, or (c) completes all effects.
Never remove it between publishing and observing its successor owner.
Exceptions and client disconnects do not imply cancellation of a possibly
launched operation. Retirement is idempotent and in finally; unknown
partially applied effects stay owned/fail-closed until reconciled, not
silently made idle.

Unbound tickets are essential: they stop a seal while a dispatch is
accepted but paused before it appears in active/spawning/admitting.
No fabricated agent slot, durable thinking row, live upsert or public
conversation key is needed for this reservation.

### 2. Resolve the canonical resource before taking an admission lane

Resolve agent ID from the same routing/identity semantics as today.
Read-only resolution may run outside the state mutex. Validate its
identity/generation under the lane before durable side effects; if the
target changed, release and resolve/rekey without partially admitting
under the old key. A session spelling or requested_session is not the
lane key: routing can select another agent.

For dispatch_queued, peek the row to identify its agent, acquire that lane,
then re-read and conditionally claim the SAME row with the existing queue
transaction. Removal, status change or ownership mismatch refuses/retries
before launch; never dispatch an agent learned from a stale row while
holding a different agent's lane. Do not hold a SQLite write transaction
while waiting for an admission lane.

Bulk recovery scans independently, claims no global execution mutex, and
submits one row through its canonical agent's lane. Existing DB claim and
recovery/backoff ownership prevent two scans from starting it twice.
Across agents there is no promised total order. Within one agent preserve
the admitted order, durable queue_seq, same-id deduplication and existing
queue-if-busy/preemption/steering policy; socket arrival time is not a new
FIFO contract.

### 3. Separate durable admission from the slow handoff

A per-agent admission lane covers preparation that would otherwise race:
session/ghost checks and binding, durable user/queue/launch receipt,
and the claim-or-queue/steer/preempt decision. Publish an exact trace/
generation and a visible spawning or effect reservation before yielding
the lane. Hold NO agent admission mutex while spawn_turn, thread/resume,
turn/start, interruption, selector/provider waits, SSE fan-out or a
callback runs. Slow preparation must similarly publish a cancellable
reservation before yielding; it cannot be hidden under a mutex.

SQLite transactions can still delay another writer; moving them out of
the runtime-global gate does not change durability or their error paths.
An ordinary later admission for the same agent waits for its predecessor's
admission decision, but a Stop can invalidate a preparation reservation
and reach its existing generation/slot fence. This requires explicit
phase checks, not a generic lock bypass.

Before receipt/handoff commit, revalidate the ticket, canonical identity,
captured USER-stop generation, release generation and exact slot claim.
If Stop won before admission finished, a later DB commit cannot acquire
the post-Stop generation and look like fresh user intent. Settle/fence
that exact attempt through existing receipt semantics; do not fence newer
arrivals or invent a safe-resend proof. Specify the exact existing error/
queued response in the implementation's intended-difference packet before
coding, with old/new deterministic controls.

Backend effects target a captured turn/client/handle identity. Looking up
'whatever handle agent A has now' at a delayed interrupt/steer is unsafe:
it could hit the successor. Keep the reviewed TP-N3 pending-start Stop
intent and TP-01/BACKENDS-07b wait-for-old-writer/session-holder rules.
A late start is interrupted by its own identity and cannot release a
newer's slot. Receipt-before-steer failure never falls through to a
duplicate preempt. Backend-specific compare/claim remains its owner's
job; the new coordinator must not hold a mutex through its native RPC.

### 4. Stop completion is ownership, not dictionary presence

begin_stop owns a ticket before installing its barrier/fence. Capture the
old work, then interrupt outside admission locks. Rollback is conditional
on the SAME barrier generation; an older failure cannot restore over a
new Stop or successor. Preserve strict interrupt failure, queued arrivals
and the user-versus-maintenance receipt distinction.

On success, publish the lease and transfer the begin ticket to it in one
short metadata transition. Schedule its existing timer outside locks.
Timer publication failure must complete/restore the barrier through an
owned failure path; it must not abandon it.

finish_stop and expiry use ONE claim operation: acquire the short
coordinator gate, look up the lease under its mutex, register its
completion ticket, then remove/mark the lease claimed atomically. Only
one claimant calls complete_stop. That ticket remains a drain blocker
AFTER removal until queue restore/filter/handoff is settled. The loser
keeps today's finish-not-found or expiry-no-op result.

Subsequent Stop generations fence stale completion/rollback, with no new
persistent schema. Preserve current queue merge/cancellation semantics;
do not solve overlapping leases by rejecting valid operations with an
unapproved new status. Exact duplicate-Stop/superseded-lease behavior is a
required control, not an assumption that STOPPING alone proves ownership.

### 5. Drain uses tickets plus an atomic final generation check

Keep OPEN, soft-FENCED and hard-SEALED lifecycle concepts. Publish the
soft fence/launch prohibition before persistence, atomically with the
corresponding TurnSlots flag. Perform persist_held, account-recovery
transfer, status/provider queries and carry-file I/O outside the short
mutex, retaining a drain-operation owner.

A seal can commit only if all pre-existing operations are accounted for,
no unbound/admitting/effect/Stop-completion tickets remain, all held work
is durably ordered, existing handover blockers/leases are empty (or
account-recovery transfer has validly replaced them), and the operation/
release generation is unchanged at the final commit. A reservation made
during the outside-lock scan invalidates that scan. Re-read current short
slot/lease facts at commit; callbacks that launch/release/handoff work
participate in the same generation protocol. Do not rely on RPC wrappers
to cover background recovery/retry/provider callbacks.

Carry/park transfer remains exact-run/queue-transaction based and bounded
by the existing parked_since deadline. Keep its durable FIFO, pause,
rollback and fail-closed uncertain-row behavior. Failure to persist or
write carry never seals or loses the held row. Fence release starts held
work outside the mutex, through the same owner/epoch checks.

A private seal result MUST distinguish PENDING_ADMISSION from SEALED and
BLOCKED. Returning an empty blocker dict for an invisible ticket would
tell RuntimeReleaseMonitor to hand off. Adapt that private monitor/server
interaction explicitly (runtime_release.py:285-330); do not add public
status/RPC fields or silently reinterpret an existing empty dict. Natural
fast-drain returns false while tickets exist. Revert/cancel changes the
release epoch so a stale outside-lock scan cannot seal the restored
runtime. Retry through the existing monitor schedule, not a busy loop.

**Release-policy decision required before implementation:** current hard
rejection covers only four methods. Proposed treatment is to classify
every native-launch-capable mutation (including compact, goal actions
that can wake work, quota recovery) into the same no-new-launch policy,
while status/live transport/Stop/lease completion stay available. This is
an explicit intended concurrency difference, not a claim about today's
code. Reviewer/lead must approve the exact action allowlist and existing
error envelopes, or require a separately owned change. No new public
payload/contract or silent exclusion of such operations from seal proof
is authorized by this design.

### Lock order, callbacks and failure rules

Coordinator metadata mutex -> short TurnSlots OwnershipLock -> lease-map
mutex, where nesting is unavoidable. Never acquire the coordinator from
inside a held turn/lease/backend/pool/SQLite lock; use OwnershipLock.defer
or finish the observation after release. Do not call user/provider code,
publish frames, wait on a condition, or do DB/filesystem/native I/O under
the coordinator. Per-agent sequencing must release its mutex before
waiting for another component. Never wait for the coordinator while a
SQLite write transaction is open.

RPC recursion/local calls on the same operation inherit an explicit
ticket/context, not an RLock-shaped exemption from registration. Background
handoffs/retries/timer completion register their own owner when not already
covered. Avoid waiting on an operation's own descendant ticket during
seal; drain's maintenance owner is distinguished from outstanding work.

Tickets track the owning worker and phase. A watchdog can report a stale
owner and recover a proven pre-effect reservation; a deadline alone must
not free a possibly live provider writer or permit replay. Use existing
operation/lease deadlines and bounded cleanup, log failures, and retain a
visible internal blocker when ownership is uncertain. No new environment
variables, database fields, blanket retries or exception swallowing.

## Deterministic controls for implementation review

All are FUTURE buildbox tests using real dispatcher/queue/RPC boundaries,
isolated DB and controlled provider waits. No tests ran for this design.
Use events/barriers and bounded cleanup; a timeout is only a hang guard.

| Control | Required observable result and reverse-order case |
| --- | --- |
| A held in native start; dispatch B and read status | B reaches its provider while A remains held; status completes. Also admit B first. Mutant restoring global/per-agent-whole-RPC serialization must fail this control. |
| Same-agent A1/A2 with A1 admission held before commit, then during start | No duplicate session binding/user row/native writer; A2 preserves queue/steer policy and order. Swap which request wins admission, and repeat same client ID. |
| Accepted RPC held before target/slot | Raise drain/seal: it cannot report sealed. Release admission: it is either properly queued/owned or refused, never lost. Reverse: seal first, then existing503 with no user/backend side effect. Mutant omitting the initial ticket must fail. |
| Stop before admission publication; Stop during native start | Correct user generation/receipt fence; no forbidden handoff or stuck successor. Release provider after Stop and prove any late handle is interrupted by its own ID. Reverse: Stop completes, fresh intent follows its normal admission/queue rules. Mutant bypassing the final stop/owner check must fail. |
| Maintenance barrier | Unchanged user-stop generation cannot withdraw an ordinary user's send. Keep existing maintenance cancellation controls green; do not import TP-N4's early state write. |
| Preemption while old writer/session remains | Successor never runs until that old owner exits; B still progresses. Old completion/interrupt cannot target or release successor. Reverse old-exit-before-preempt. |
| finish_stop vs expiry | One completion claimant, at most one queue handoff; drain blocked after dictionary pop until completion finishes. Exercise both winners, duplicate finish, second Stop, and exception after claim. |
| Stop failure with newly queued work | Exact snapshot/pause restored only if still owned; new arrivals retained in order; later handoff once. Reverse newer Stop/owner before old failure. |
| Queue edit/remove/manual send vs Stop | Edit through actual Host lease boundary; removed/cancelled row never starts, kept/fresh rows retain order and pause rights. Race row lookup with claim/remove; wrong-agent ticket never authorizes dispatch. |
| Concurrent bulk recovery/manual send | One native start per durable row; other agents independent; current recovery backoff/account cap unchanged. |
| Soft fence/seal/revert/carry | Both fence-before-admission and admission-before-fence; generation dirty during scan/carry refuses seal; carry-write failure and rollback retain all held work in FIFO. New native-launch methods cannot sneak past the agreed hard-seal policy. |
| Failures, disconnect and lock reentry | Exceptions at reservation/binding/receipt/claim/provider/lease/timer-publication seams release or transfer ownership exactly once. Client timeout does not cause a duplicate launch. Callback reentry cannot deadlock; no SQLite transaction waits for coordinator. |
| Live-state contract | No early thinking write or conv re-key introduced; existing event payloads/final state remain valid. Retain TP-N4 as a known, parked low issue. |

Run the existing runtime Stop lease/queue, graceful drain/carry, queue
recovery, exact-owner preemption and runtime-dispatch characterization
controls as regressions. Existing status-only TP-02 reproduction is useful
but not sufficient for the acceptance controls above. Mutation checks are
part of the planned proof, not yet executed claims.

## Integration and implementation route

1. A2 reviews this design; lead signs exact intended concurrency differences
   and resolves the native-launch hard-seal policy. No implementation until
   after big merge #1. No request for acceptance of the HIGH finding.
2. On then-current main, add the deterministic failing controls first. Add
   explicit coordinator/ticket and dispatcher admission/effect seams in a
   narrow fix series; preserve old wire and stored formats. Validate backend
   exact-handle and Stop/callback lock order with owners A/A2.
3. Main is authoritative. Coordinate new class helper names/globals with S4
   BEFORE landing. Update reviewed state/method recipes for every new owner
   (Admission/Claim/Launcher/Handoff/Drain/Stop as actually touched), refresh
   changed-body G0, then regenerate from main at the next integration merge.
   runtime_bridge's function-size extraction/forward-port watch must be
   rebased by its owner, not patched around in generated output. No handwritten
   edits of S4's generated modules.
4. Author G1/G2, exact A2 SOURCE YES and captain G4 remain required, with
   shared generated identity/gates and final target checks. Source review or
   this design is not proof of runtime concurrency, live deployment, or a
   shipped fix.

TP-N4 stays parked on refactor/f1-tpn4. None of its provisional early
thinking, failure marker/lifecycle changes or unsigned live conv key is a
dependency here. If separately revisited after big merge #1, it must use
the admitted operation's exact identity and receive its own design/gates.
TP-02 cannot count its speculative states as blockers or as Stop proof.
