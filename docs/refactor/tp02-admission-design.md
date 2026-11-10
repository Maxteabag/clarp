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

**Original intent ordering (A2 finding 1).** Every initial reservation gets
an immutable sequence s from the coordinator, before it is unbound. Every
USER Stop publishes a sequence cutoff for its canonical agent at its
barrier transition. Resolving or rekeying D to A compares D.s with A's
retained successful USER-Stop cutoff: D.s <= cutoff is old intent and may
not launch as a fresh post-Stop request. Never replace D.s or sample a new
per-agent generation to make that comparison pass. An ordinary withdrawn
send uses the existing refusal; already acknowledged explicit queue work
stays durable/paused rather than being lost or relabelled fresh. Keep s through
every rekey, then bind the existing durable user-stop generation for the
receipt/handoff checks. A Stop committed after binding is caught by that
later check too.

A cutoff is provisional while barrier/pause installation can still roll
back without a durable user fence. Remove that provisional cutoff only if
its own installation failed before a durable user-stop generation/receipt
fence committed, and only if no newer Stop replaced it. Once the durable
user fence committed, retain its cutoff even if interruption later fails:
today rollback of pause/slot does not decrement the user-stop generation
or undo fenced receipts. A maintenance Stop publishes no USER cutoff and
does not withdraw old user intent. Its barrier still orders handoff.

Retain cutoff history while any outstanding unbound/rekeyable reservation
could precede it; prune only below a proven reservation watermark, never
on a timeout alone. A request resolving from A to B keeps s and checks B's
cutoff; identity revalidation cannot transfer already committed work to B.
On runtime death there is no transferable in-memory RPC authority: resume
only existing durable producer/receipt work with its saved identity and
fences. This is not a new cross-runtime timestamp or wire field.

Forward trace: reserve D(s=10), block lookup, USER Stop(A,s=11) commits and
finishes, D resolves A: refuse the old attempt through the signed existing
error/receipt semantics, no provider handoff. Reverse: Stop finishes then
D(s=12) resolves A: ordinary fresh-intent rules apply. Repeat with rekeying,
maintenance Stop, pre-durable failed Stop and post-durable interrupt failure.
The future mutant that samples the current generation after lookup must
fail, as must premature cutoff pruning.

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

**Overlapping payload ownership (A2 finding 2).** A per-agent Stop chain
owns saved payloads independently from the latest barrier and individual
RPC/timer claimant. Each generation record retains its original trace,
saved queue segment, pause baseline/arrival rights, pending lease deadline,
cancelled-trace set and parked/taken-over cleanup obligations. The lease
refers to that record; supersession transfers the record into the chain,
not into a discard list. There is one current barrier owner but potentially
several unfinished payload owners.

| Transition | Payload and barrier result |
| --- | --- |
| S1 begins | Chain takes Q0 out of the slot; S1 owns interruption/publication and its saved segment. New arrivals form Q1. |
| S2 begins before S1 completes | Append Q1 as S2's segment; retain S1/Q0 and every unfinished cleanup. S2 becomes the barrier owner. Q0 remains pre-Stop held; Q1 is now also captured pre-S2 and loses its pre-S2 bypass right, exactly as prepare_queued_for_finish does to a captured snapshot. New arrivals form Q2 and retain only the usual post-S2 fresh-intent rights. |
| Valid finish(S1) after S2 | One claimant merges S1 cancelled IDs and completes S1 cleanup; mark that record ready. It never restores/releases S2's barrier. The earlier Host result is consumed here even though its generation is superseded. |
| finish(S2) before S1 is ready | Complete S2's own payload, but retain the barrier/chain until all earlier payload obligations are ready or expire through their existing timeout path. |
| Expiry competes with finish(Si) | Exactly one claims Si. Expiry uses the existing empty cancellation result, revalidates durable removals, and marks Si ready; no second restore/handoff. A losing/duplicate Host finish retains today's not-found result, not an invented success. |
| S2 installation/interrupt fails | If S2 still owns the barrier, roll back its control transition to the surviving S1 chain, carrying all arrivals/cleanup. If S1 is already ready, complete the chain only after S2 rollback is accounted for. Never restore a terminated old trace or discard Q1/Q2. |
| S1 failure arrives after S2 | Settle S1's failure/data obligations; no older slot/pause rollback over S2. A committed USER cutoff stays committed. |
| All records ready | Current barrier owner alone filters the union of accepted cancellations, applies cleanup once, and merges kept Q0,Q1,Q2 in original order with unchanged Stop pause/fresh-arrival rules. Then hand off once through ordinary generation checks. |

Final assembly re-reads each durable row/status before launch; it cannot
resurrect a row the Host already removed. Cleanup is keyed by the existing
row/trace identity and is idempotent. Preserve the captured cancellation
sets until their park/taken-over cleanup is known complete, including a
transient cleanup failure; a record cannot disappear merely because its
lease was popped. Memory-only work retains an explicit chain owner until
it is restored or made durable.

Conservation trace: Q0 precedes S1; Q1 arrives before S2; Q2 arrives after
S2. Finish S2 then finish S1 cancelling a parked/taken-over member of Q0:
only kept(Q0,Q1,Q2) can hand off, once, after both payloads are ready. Reverse
the finish order and each expiry winner; fail S2; deliver S1 cancellation
after supersession but before its lease is claimed. All accepted payloads
must survive. A finish that loses to expiry is not an accepted late payload;
retain the existing bounded lease contract/404, and revalidate any already
committed durable removal. Do not promise to undo effects launched before
a cancellation arriving after every lease deadline.

This makes overlap conservation explicit and can delay the latest handoff
until an earlier lease finishes/expires. It is a separately stated intended
ordering refinement APPROVED IN PRINCIPLE by lead642. The exact implementation
commit still needs A2 code review and the lead's exact intended-difference
signature. It is not authority to change lease timeouts, duplicate-finish
responses or public payloads. Mutants discarding S1's snapshot, forgetting
cleanup after pop, or releasing S2 from S1 fail.

The current production lease deadline is **30 seconds from the existing
lease timer's publication/start**, not 30 seconds from a later Stop or finish:
runtime_bridge.py:467 defaults stop_lease_timeout to30.0; :489 retains the
existing minimum clamp; :663-669 starts that timer after lease publication.
server/runtime.py:47 does not override it. Tests may inject shorter clocks/
deadlines; that is not a production timeout change. Each earlier record keeps
its original deadline. Supersession, waiting, retry or a duplicate finish must
never restart/extend it. The added payload-readiness wait ends when Host
finish arrives or, at worst, at that earlier lease's existing deadline. The
usual expiry/cleanup/owner checks still execute; this is not a new wall-clock
SLA for an OS timer, storage or an unrelated live provider owner.

Specify a virtual-clock control: S1 publishes at t0 with deadline t0+30;
S2 finishes at t0+5 while S1 is pending. No handoff before S1 finishes, but
if no finish arrives, its existing expiry claims readiness at t0+30 with no
new grace period. Exercise both finish orders, both expiry winners, S2
failure, cancelled parked/taken-over rows and fresh Q2. Verify kept Q0 then
Q1 then Q2, existing pause/fresh-arrival rights and ONE final handoff. A
mutant extending the older deadline or skipping payload readiness must fail.

Emit ONE log line on the transition into waiting for earlier payloads (not
each poll): event stopPayloadWait with current/waited trace IDs, barrier/
payload generation IDs and existing deadline/remaining duration. No prompts,
queue text, transcripts or other content. Duplicate finish/polls do not log
the same wait again. Test log cardinality/identifiers and absence of content
using synthetic inputs; no new public event, payload or environment variable.

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

## Sealed runtime policy (lead 636)

Approved in principle as a SEPARATE, independently revertible commit in
the TP-02 candidate. It needs its own deterministic RED and A2 review, then
the lead signs its exact intended difference on the exact commit. It is
not yet signed code. Reverting it restores the previous four-method
policy; do not then claim the complete no-new-launch proof still holds.

At the SAME dispatch_request lifecycle point as :519-525, before method
body validation, lookup, claims or provider activity, a denied operation
returns EXACTLY the current envelope:

    {"ok": false, "status": 503,
     "error": "agent runtime is draining for an update"}

No new status, error text, fields or protocol version. Unknown RPC methods
remain 404, not a blanket 503. Soft-fenced operations may still be admitted
durably as held work; the matrix below applies to the hard SEALED phase.

| Method/action | Sealed policy | Why |
| --- | --- | --- |
| dispatch, dispatch_queued, recover_queued, steer | DENY | Existing four; may start/continue provider work. |
| compact | DENY | Starts an interactive native process/worker. |
| recover_janitor_quota | DENY | May run selector/recovery and resume turns. |
| explanation_command | DENY | Conservative native-reader-capable operation, explicitly included by 636; do not bootstrap a reader after seal. At the inspected pin answer/resolve is cache-only; this narrower current implementation is not a general permission for future native-reader fallback. |
| goal/start | DENY | Activates native work and may bootstrap its app-server. |
| goal/resume | DENY | Activates native continuation and may bootstrap. |
| goal/pause | DENY | Although reducing goal activity, current path first obtains _client and _ensure_loaded; it can create/reload native work before pausing. |
| goal/clear | DENY | Same native client/load path before clearing. |
| goal/get | DENY | The outer goal() obtains _client even for get; a read can start a new app-server/reader. No speculative cache-only API is added. |
| goal/unknown or missing action | DENY | Same dispatch lifecycle point; current enum validation is after client creation. When OPEN retain existing validation/errors. This is not an unknown RPC method. |
| release_leaked_slots, repair_slot | DENY | They can hand off ownerless queues/recover work, not merely edit a map (turn_dispatch.py:3708-3778). Internal drain cleanup can settle slots but any child launch is independently fenced. |
| status, live_snapshot | ALLOW | Snapshot/read; must not create a provider as a side effect. |
| live_stream | ALLOW | Existing streaming transport bypass; ends on runtime close. |
| live_observe, live_patch | ALLOW | Existing projection operations; no new provider launch. |
| interrupt, interrupt_any, begin_stop | ALLOW | Stop existing work using its captured identity. No bootstrap of a missing native client. |
| finish_stop, timer expiry | ALLOW | Complete cancellation/lease obligations; any descendant queue launch must separately pass the same generation check and may remain held. |
| cancel_janitor_run | ALLOW | Cancels/reduces exact-run work; does not gain a native-launch exception. |
| release_agent | ALLOW | Stop/delete; captured-owner guards and child-launch fence remain. |
| any unknown RPC method | Existing 404 | Do not accidentally turn validation into launch permission. New methods require an explicit classification before registration. |

All known goal actions are listed individually deliberately: pause/clear/
get are not automatically safe merely because their intent sounds like a
read or reduction. Stop's internal pause/cleanup against an ALREADY owned
native process remains available through its Stop path; it must not call
a client factory to turn that exception into a new process launch. Pin and
recheck these backend call paths after the Sunday fixes before coding.

### Automatic work conservation and re-arm owners

A 503 is not completion, cancellation, quota exhaustion or a permanent
goal failure. Refused user-initiated operations behave like today's refused
send: existing error propagation, no hidden automatic replay. Automatic
producers instead retain their original intent/identity until a canonical
admission or valid superseding cancellation is known. An in-memory wakeup
is an optimization, never the sole post-handover truth.

| Trigger | Mechanism on refusal/lower/handover | Deterministic future control |
| --- | --- | --- |
| Host goal wake via task_goal_recovery.tick | Existing continuation.request_id and goal revision remain; its exception path records retry/due_at, not admitted. Existing tick retries the SAME ID after lower/new runtime and revalidates owner/native binding/Stop/approval. | Refuse one wake, lower or restart, advance clock: one admission with same ID; pause goal before retry: none. |
| Automatic schedules, heartbeat/leader/Janitor/decision delivery through dispatch_adapters | Keep the producer's durable due/claim unconsumed until admission. Ordinary prompt work without such a producer claim must be placed in an EXISTING normal queued_turns row, with stable client ID/origin/owner, before its callback can finish. Existing recover_queued after lower/startup drains it; no special control command encoded in chat text. | Refuse one-shot and periodic producers, restart runtime, recover: each retained eligible intent once, no acknowledged work lost; cancelled/superseded producer never fires. |
| Queued handoff, retry timer, provider completion callback | Preserve the current spec/queue row, original trace and existing backoff/cap; park ordinary unlaunched prompt work using existing queue/park format before losing the callback owner. Lower schedules ownerless and durable recovery. If partially launched, keep backend ownership, never reclassify it as unsent. | Fence after decision but before deferred spawn; expire callback thread/restart; kept row runs once, old writer/Stop guard still wins. |
| Janitor quota recovery, automatic mode | Recompute still-eligible exhausted-provider/unfinished-owned-work recovery on subsequent quota ticks independently of whether a NEW notification receipt was inserted. The current code only calls request_recovery inside the inserted-receipt branch; that is not sufficient re-arm. Preserve existing attempt/park/carry identity and parked_since cap; revalidate owner generation/mode each time. Untransferable work blocks handover instead of being dropped. | Refuse after receipt insertion, repeat same quota window (no new notification), lower/new runtime: selector requested once for retained work; disable owner before retry: none. |
| Janitor quota recovery, approved mode | Existing durable accepted artifact remains unconsumed: recover_approved already marks its settings key only for recovering/nothing_to_resume, never for 503. Next quota tick retries the same approval after lower/new runtime and verifies current owner generation. | Refuse then restart/tick, same artifact once; revoked/stale generation prevents recovery. |
| Automatic compaction | No Host automatic compact_session caller exists at this source: only the user HTTP path calls it. Native CLI auto-compaction is part of an ALREADY owned provider turn, so drain retains that owner rather than refusing/lossily replaying a new RPC. If the implementation-time census adds a Host automatic compaction producer, it MUST retain its original durable parent job/claim until success; add its specific re-arm control before enabling this policy. Do not invent a new automatic feature or persist control commands in chat rows. | Native turn compacts while fenced: seal waits for owned turn. For any newly found Host producer, refuse before _active publication, lower/restart and prove its durable parent retries once; no producer means no fictitious passing test. |
| Direct automatic goal start/resume | None found in the inspected Host caller graph; Host automatic goal wakes use dispatch and the continuation row above. An implementation-time direct automatic caller must retain its durable parent goal action/revision until ACK, then re-enter the gate; user goal RPCs are not silently auto-replayed. | Caller-census guard plus any real producer's refusal/restart/supersession control; do not infer durability from a native mirror written only AFTER its reply. |
| Automatic command explanation/native reader | Existing tool_explanation_queue is the durable owner, but currently commands.expand runs BEFORE durable_queue.request (:tool_explanations.py:250-314). Move any reader-capable expansion behind enqueue/claim of the original activity/target demand, using existing activity_json/demand IDs; rederive keys/parts through reviewed existing rekey mechanisms after expansion. RuntimeClient.explanation_command/_ask must distinguish the existing drain503 internally instead of collapsing it into an empty successful cache miss. Return/release the claim with durable_queue.abandon and retry on lower/new runtime while demand is current. No new stored fields or silent success from a clipped fallback. | Refuse reader BEFORE expansion, verify original demand was persisted first, restart/lower: one eventual completion; released/stale demand does no provider work. Cache-only source behavior stays separately covered; a plain periodic-poll assumption is insufficient. |
| Reconcile leak release/slot repair | Existing periodic reconcile/ownerless recovery retries; a sealed refusal never asserts that a live slot was released. Actual queue work remains in the existing durable queue/park. Drain's internal cleanup does not launch descendants past the fence. | Refuse repair with durable waiting row, lower/restart: one safe repair/handoff; live session holder never reclaimed. |

The implementation must audit every actual producer's claim/ACK boundary;
"the scheduler will run again" alone is not evidence for a one-shot event.
If an automatic producer has neither existing durable intent nor a compatible
existing prompt queue representation, add a specifically reviewed recovery
owner BEFORE enabling refusal for it. Any new stored representation needs
the contract owner's reader-before-writer rollout; no such format change is
approved here. Do not claim the policy complete with a missing re-arm owner.

The same last-moment release/owner generation check is required at
_spawn_attempt_claimed, the steering handoff, compaction worker publication,
native goal/client startup, account-selector resume, _retry_handoff,
_resume_and_spawn, _resume_after_account_switch, _schedule_retry's callback,
start_held_work, and _finish_slot/Stop-completion deferred launches. Audit
direct backend calls on the actual implementation base. Publishing a deferred
child must transfer the parent's ticket to the child before release; no
release-to-callback interval may disappear from the seal proof. Provider
events describing existing work do not get a fresh-launch exemption.

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

1. A2 reviews this design and its exact matrix/re-arm obligations; lead636
   is in-principle approval, not a code signature. The sealed-policy commit
   gets its OWN RED, A2 source review and THEN the lead's signature on that
   exact commit. Keep it independently revertible inside the candidate.
   No implementation until after big merge #1; no HIGH risk acceptance.
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
