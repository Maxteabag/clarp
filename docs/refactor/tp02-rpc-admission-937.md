# TP-02 / 937: narrow runtime RPC admission on the current layout

Status: FIRST DESIGN, review-first; no implementation or test execution. Exact main
base: `f666fb0db240513ae6dae1eda8227046b4001088`. Owner rf-s4c-8be3;
reviewer A rf-review-core-bbc1. Worktree `fix-tp02-rpc-admission`, branch
`fix/tp02-rpc-admission`. Land behind fix237 queue-933 AND BACKENDS-01/TP-N3,
then re-pin the inventory and proposed hooks to actual main. First draft ETA
09:30 Europe/Oslo, 8 November; this note supplies it early.

## Outcome and intended difference

A blocked RPC/provider operation for A must not serialize status or unrelated
agent B, and must not prevent A's Stop from reaching its existing owner fence.
Same-agent durable admission, exact target ownership, client retry deduplication,
Stop payload conservation, and drain's no-invisible-work proof remain required.
This fixes TP-02; backlog38 status timeouts and933's observed runtime read/write
hold are related hypotheses, not root causes proved or automatically closed here.

Intended difference: independent operations can overlap, so status/B/Stop latency
no longer includes an unrelated RPC body's wait. There is no new wire field,
method, status code, error text, timeout, path, environment variable or stored
format. Concurrent schedules that were previously impossible are an intended
interleaving change and require deterministic owner proofs, not just timings.
No claim of a wall-clock status SLA: its actual status/provider, DB and hub locks,
backpressure and OS scheduling can still block. No unsafely cached fake status.

This is NOT permission to expand the current four-method sealed rejection set,
rework TP-N3, repark failed Stop receipts, change USER queue/steering priority,
or claim BACKENDS-N2/TP-N4 fixed. Item36 remains open. Earlier TP-02 design288e377f
and U10 agreement b93b28bb/47d9e822 are retained design inputs, not current code
approval. Their expanded sealed-method policy (lead636) and overlapping-Stop
ordering refinement (642) are separately identified below; no silent rider.

## Exact baseline evidence

`server/lib/runtime_bridge.py` at f666:
- `dispatch_request:537-544` owns `_admission_lock` throughout draining check and
  `_dispatch_request`. ThreadingMixIn's separate handlers consequently serialize.
- `begin_drain_if_idle:827`, `begin_admission_fence:852`,
  `release_admission_fence:856`, `seal_now:911`, `seal_if_drained:948` and
  `cancel_handover:991` use the same RLock. Slow persistence/status/carry operations
  run inside it today. `start_held_work` already runs outside it.
- `_stop_lease_lock` is separate. `_finish_stop_request:806` pops under that lock
  then completes outside it (currently still inside the outer RPC gate).
  `_expire_stop_lease:1020` pops and completes WITHOUT the outer gate. Dictionary
  absence is therefore not evidence that all Stop completion effects ended.
- Decode/response write and the `live_stream` path are outside dispatch_request.
  `_RuntimeRequestHandler.finish` independently closes this thread's DB/telemetry
  connections after stream/response flush. That cleanup remains intact.
- Reproduction `reviews/repro-A.md`, independent TP-02 status-vs-blocked-body
  test, establishes serialization. It does not establish933's data-loss cause.

The exact current hard-drain set is dispatch, dispatch_queued, recover_queued,
steer. Rejection precedes method-body validation. The capability prelude for
interrupt/interrupt_any/steer/begin_stop/release_agent remains in the same error
precedence. No current wire validation or default is redesigned.

## Method/resource inventory

| RPC / entry | Concurrent portion and real owner | Required ordering |
|---|---|---|
| status | Capture draining metadata briefly; existing status_provider owns its snapshot | No admission/agent execution lane; snapshot consistency, existing errors |
| live_snapshot | Existing hub lock/flush; observational but not literally side-effect-free | Hub order only; no launch gate or provider mutex |
| live_observe, live_patch | Existing hub state-id ordering / hub lock | Their own projection sequence, independent of admission |
| live_stream | Existing subscribe/stream path already bypasses dispatch | No change |
| unknown method | Existing404; no work ticket needed | Preserve error shape |
| dispatch | Immutable initial reservation; resolve canonical agent; durable admission and claim/queue decision; then native effect | Per-agent PREPARATION/ADMISSION order, same client ID dedup, exact owner effect; never whole-call agent mutex |
| dispatch_queued | Reserve before row lookup; resolve actual agent, re-read exact queue identity at claim | Current933 protected claim and that agent's admission; no stale row learned under wrong key |
| recover_queued | Existing scan/recovery lock, parent reservation; rows use same admission machinery | Per-row canonical agent and existing queue sequence; no global execution mutex |
| steer | Current receipt-before-steer and captured owner | Short agent decision and owned slow effect; Stop may fence it |
| interrupt, interrupt_any | Exact captured target; backend's current owner/hold semantics | Agent control intent, not a lock across native RPC |
| begin_stop | Reserve before barrier; existing user/maintenance distinction; captured interrupt; lease publication | Agent control transition, not behind ordinary launch/admission wait |
| finish_stop / timer expiry | One lease completion claimant; retain completion owner after dictionary pop | Exact lease/snapshot and its existing deadline; no double completion/handoff |
| release_agent | Exact agent lifecycle transition then captured interrupt and deletion | Same-agent lifecycle exclusion; cannot delete/rebind a successor |
| release_leaked_slots, repair_slot | Existing per-agent exact-owner/liveness guards | Agent repair decision; no provider wait under global admission gate |
| goal | Canonical agent/native binding plus existing stage2 goal coordinator | Agent effect ownership; no new goal/stop policy or latest-client substitution |
| compact | Resolve session to actual agent; existing compaction publication/worker | Agent/native-session admission through worker publication, not whole worker lifetime mutex |
| explanation_command | Local tool_explanation_commands.answer -> resolve/redact, cache TTL/LRU touches under its existing lock; NO provider RPC | Cache-owner ordering only, no new provider lookup/refusal |
| cancel_janitor_run | Resolve run to actual agent, retain exact run generation | Agent maintenance/control claim, current user-vs-Janitor permission |
| recover_janitor_quota | Provider/owner/generation recovery owner | Existing quota coordinator; resumed agents enter their own admission; not a global agent lane |

This inventory covers every current _dispatch_request branch. API routing, error
precedence and which methods may act after hard seal remain explicit review cases.
No accidental admission bypass for a new or forgotten method: the policy table is
closed over the actual RPC branches, with unknown method retaining404.

## Proposed boundaries

### 1. Short runtime coordinator, no global reader/writer execution lock

Keep one existing-purpose runtime metadata mutex (replace the RLock's whole-body
scope). Under it ONLY: phase/epoch, operation issuance/retirement and short
metadata state. Do not hold it for lookup, SQLite, filesystem, providers,
status_provider, hub flush, logging, condition wait, callback or response writes.
A conventional reader/writer lock held over RPC bodies is rejected: slow read
or write work would still prevent Stop/drain and recreate starvation.

Each side-effecting RPC registers an operation BEFORE its first potentially
blocking lookup/DB action. Its immutable process-local initial sequence survives
canonical-agent binding/rekey; it initially may be unbound. Reads use no work
reservation. Existing hard-drain rejection remains before validation for the
same four methods. Retire/transfer in finally, including malformed params,
exceptions, client disconnect after submit and lease timer publication failure.
No success/retry can be inferred from timeout or uncertain write failure.

Operations remain visible until work completes or ownership has transferred to
an existing slot, durable queue, compaction/recovery owner or Stop lease. The
parent survives until a deferred child is registered, BEFORE parent retirement;
no callback-publication gap. No global forever-growing history: reference-count
live operations/lanes; prune per-agent Stop cutoff metadata only below the oldest
outstanding bindable reservation sequence, never an arbitrary age timeout.
No operation context is serialized into RPC/DB/auth or confused with TurnSlots'
slot identity. Existing U10/TP02 parent-to-child agreement applies.

### 2. Canonical-agent admission and slow-effect separation

Resource keys are actual agent IDs (or provider/owner recovery keys), never an
unresolved requested_session spelling. Resolve outside the metadata gate; before
admission, revalidate routing/binding/row identity under that resource's short
admission phase. No SQLite write transaction waits for an admission lane.

Ordinary admissions for one agent make one durable/idempotent decision in
sequence. Existing `_serialize_client_retry` remains the same-ID dedup owner;
existing queue_seq and933's positive launch classification remain authoritative.
This is NOT a new socket-arrival FIFO promise. A preparation reservation keeps
later same-agent admission ordered while slow preparation runs; Stop is a control
transition that can invalidate/fence it rather than waiting behind that effect.
After publishing the exact slot/spawning/effect owner, ordinary lane ownership
ends BEFORE native session/start/steer/interrupt/selector/worker I/O. A physical
per-agent RLock across submit/spawn or goal RPC is explicitly rejected.

A dispatch initially reserved before a USER Stop must not resolve later, sample
a new generation and masquerade as fresh post-Stop intent. Retain original
sequence and compare it to the successful USER cutoff plus current existing
receipt/user-stop generation. Failed pre-durable Stop can undo only its own
provisional cutoff; durable user-stop/receipt fence remains after interruption
failure, as current code does. Maintenance Stop never publishes a USER cutoff.
Use current DispatchError/fenced/queued outcomes, never a new safe-resend rule.
Exact outcome mapping is a reviewer-required pre-code matrix, not invented here.

Minimum non-runtime seams likely required for COMPLETE TP-02: actual canonical
binding/admission boundaries in `AdmissionMixin._dispatch_owned`; the final
owned effect check in `_spawn_attempt_claimed` and existing steering/handoff
entry points. runtime_bridge alone cannot know when an unresolved command becomes
one agent or when submit exits durable admission and enters a native wait.
These are small explicit owner callbacks/context hooks, NOT a second admission
implementation, monkeypatch/proxy backend, or wrapping a whole generated facade.
Land behind fix237's933+TPN3, agree exact changed definitions and latest homes
with that owner before any edit. Initial implementation should keep the bulk of
new logic in runtime_bridge; changes elsewhere need that named owner agreement.

### 3. Stop and lease completion

Stop bypasses ordinary admission/effect waiting, reaches the current TURN_LOCK
barrier and existing user/maintenance receipt fences, then interrupts its exact
captured native owner outside coordinator/turn/lease locks. Current BACKENDS-01
pending-start and late-ID rules remain backend-owned. No repark helper or new
rollback DB write is authorized (backlog36).

finish and expiry atomically choose ONE completion owner; it remains a seal
blocker after the dictionary entry is popped and until complete_stop's effects
are transferred/settled. Timer completion uses the same ownership protocol as RPC
finish without waiting on a global RPC mutex. Existing timeout is30s from the
original publication, clamp unchanged; no extension/reset by another Stop.
Public StopLease and duplicate-finish404/expiry-no-op remain unchanged.

Overlapping Stop saved payloads cannot be discarded by a newer barrier or restored
by an older failure over a successor. Reuse the earlier b93/47d9/288 ownership
split with fix3: TP02 owns control/lease/publication; TurnSlots owns exact bounded
slot decisions. A concrete exact Stop generation/claim/rollback adapter is a
PRE-CODE design requirement with A and fix3/A2; do not pretend current bool
restore_stop proves a generation fence. Lead937 explicitly excludes the earlier642 ordered-payload refinement. Keep
today's shipped Stop semantics. If A proves a correct concurrency fix requires a
part of642, STOP for a short exact intended-difference memo and lead decision;
there is no rider permission. No authority to close backlog36 via this fix.

### 4. Drain without invisible work or I/O under metadata locks

A fast-idle/seal decision must see all unbound admissions, preparation/effects,
Stop completion owners and existing leases plus current provider/slot/held-work
blockers. Soft fence and phase/epoch are published before slow persistence or
carry scanning; retain a drain-operation owner. Do persistence, headline flush,
recovery park transfer, status and carry I/O outside the metadata mutex. At final
commit, recheck short owner facts AND unchanged epoch/generation; an admission or
completion ownership change during the scan invalidates it. No synthetic agent
slot or public blocker ID can stand in for an unbound operation.

The existing methods use empty blocker dict as SEALED. An invisible pending
reservation CANNOT return{} or the monitor shuts down. Use a private explicit
PENDING/OWNED/SEALED result between runtime_bridge and RuntimeReleaseMonitor
(or an equivalent reviewer-agreed internal API); keep public status/RPC shapes.
This is the other likely small non-runtime_bridge seam. Existing monitor tick
retries, drain budget, natural-idle path, account recovery carry and revert remain.
A stale scan after cancel/revert cannot seal the new epoch. Start-held work and
any child handoff occur outside locks with parent-to-child owner transfer.

Lead937 SETTLES hard-seal scope: ONLY the current four rejected RPCs stay
rejected. Other side-effecting methods get reservation/accounting with NO new
refusal. The old636 expansion is OUT and separately assigned backlog45. The
acceptance bar here is drain safety NO WORSE than current shipped semantics, not
a new all-method post-seal no-launch guarantee. No status-code reuse conceals a
new policy; no blanket closure of backlog45 is claimed.

## Lock order and lifetime invariants

Issue/claim under coordinator, release it, then obtain short canonical-agent
admission/TurnSlots/lease state as appropriate. Never enter/wait for coordinator
while holding TURN_LOCK, slot lock, backend/pool/claims/move lock, lease lock or
SQLite writer. For a slot callback already under TURN_LOCK, retain its existing
parent and publish any child through an after-outermost-release path BEFORE
retiring the parent; deferral alone is not an ownership bridge. No coordinator
held while calling arbitrary owner code. Review explicit acquire/release call
traces; no presumed GIL atomicity, condition fairness or recursive-lock exemption.

Any short nesting that cannot be avoided gets an explicit total order in the
accepted implementation API; it is not invented by a diagram. Protect per-agent
registry lookup with metadata only; never hold registry while waiting for a lane.
Do not add a new SQLite transaction or change current transaction/RETURNING/TLS
lifetime to make a lock proof convenient. S42/native callback mirror ordering stays
with the original owner; status concurrency cannot move those writes to requesters.

## Deterministic proof plan, captain execution only

Use existing real RuntimeRPCServer + RuntimeClient/isolated DB/protocol-fake
boundaries, events/barriers with bounded cleanup (timeouts only hang guards).
One primary regression owner per contract; no helper self-comparison or source
inventory test. Extend existing runtime_bridge/drain/Stop tests where possible.
No local sockets/tests. Reviewer A upfront request rf3c135e9d46e is fulfilled; criteria
`reviews/tp02-937-A-upfront-criteria.md` read in full. It grants no design/code approval.

| Independent control | Expected observations; credible mutant |
|---|---|
| Block A after real dispatch enters slow provider; request status/live snapshot and dispatch B | Responses/B provider entry occur while A's release event remains unset; restoring global whole-RPC lock fails |
| Hold A1 before durable admission; submit A2 and same client-ID retry | One receipt/user row/native writer, existing queue order and steer behavior; wrong lane key or early lane release fails |
| Hold A's original reservation before target resolution; Stop A; then resolve | Old intent cannot reappear as fresh; reverse Stop then new request follows normal USER policy; resetting sequence after lookup fails |
| Stop during A's pending native start, plus unrelated B | Existing TP-N3 exact late-ID/timeout holds and durable paused follow-up preserved; Stop does not wait for ordinary RPC lane; bypassed fence fails |
| Hold admitted RPC before first visible slot; attempt fast drain/seal | Not sealed/no shutdown; releasing request yields existing owned/queued/refused result; omitted reservation fails |
| Mutate reservation/release epoch during outside-lock blocker scan/carry | Stale scan cannot seal; cancel/revert preserves held FIFO once; omitted epoch recheck fails |
| finish_stop vs expiry, pop then block completion, concurrent drain | One completion/handoff, drain cannot overlook popped owner; both winners/duplicate404/exception after pop; premature retirement fails |
| S1/S2 payload/rollback, saved Q0/Q1/Q2, original lease deadline | No old rollback over new barrier or lost snapshot/cancellation; exact proposed adapter must satisfy independent owner trace, not table oracle |
| dispatch_queued removal/claim/release plus concurrent recovery/manual send | Exact933 identity/positive-unlaunched guard, no duplicate execution or wrong agent; unguarded row rekey fails |
| DB, provider, validation, timer-publication and writer failure | Ownership retires/transfers once; no false idle/retry from ambiguous commit, same primary errors; leaked ticket/duplicate release mutants fail |
| Nested callback/lease/release paths while an RPC is held | No lock inversion/own-descendant wait; existing provider callbacks and M01 handler TLS cleanup remain correct |
| Precedence/wire matrix | Current capabilities, gate-before-validation, unknown404, exact results/errors and lease defaults; forbidden payload/timeout changes caught independently |

Existing owner suites to retain: tests/unit/test_runtime_bridge.py,
test_runtime_release.py, test_graceful_release_drain.py,
test_natural_release_drain.py, test_drain_recovery_transfer.py,
test_drain_pause_parity.py; current933 queue tests and then-landed TP-N3 native and
minimal USER/N1 controls. One captain-approved focused job first, read actual RED
and GREEN; captain owns G1/G2/exact landing CI. No duplicate author jobs, cargo,
Host restart or live/provider experiment. Queue933's focused/G1/G2 never prove
TP-02 by proxy. New long-lived jobs use the program's durable launcher via captain.

## Review and delivery sequence / open decisions

1. This exactf666 source map/design to A; obtain upfront acceptance before code.
   Coordinate with fix237 (933 then TP-N3, no concurrent generated-file edits) and
   fix3 (existing TP02/U10/S02/S03/S08 boundary). No implementation while shared
   operation/Stop/seal API or owner boundary is unresolved.
2. Refine concrete ticket/agent-lane/Stop-completion/deferred-child API and exact
   outcome matrix with A. Lead937 keeps the four-method seal policy and shipped Stop ordering;
   do not reopen636/642. If A proves a correct fix requires changed Stop semantics,
   send only that precise intended-difference memo to lead before code.
3. Re-pin after both queue933 and BACKENDS-01/TPN3 land. Named narrow maintenance
   agreements for actual hooks, then RED tests, then production; no code in this
   design commit. Keep runtime status-only mitigation independently revertible
   if landed early, but explicitly PARTIAL: it cannot close TP-02 by itself.
4. Full staged implementation + independent controls/mutants, A exact SOURCE YES,
   captain all required gates and main G4. High finding closes only on the complete
   qualified fix; no risk acceptance or source-design substitution.

Known limits/open design gates: exact design acceptance under A's delivered criteria; actual post933/TPN3 source;
canonical admission/effect hook and a preserving Stop completion adapter. Lead937
approves private PENDING/OWNED/SEALED and narrow named hooks as INTERNAL seams,
subject to A and file-owner agreement; it settles existing seal/Stop policy, not
the concrete implementation. These are pre-code design questions, not excuses
to accept TP-02. Source note delivered before09:30; refined
review-ready API draft ETA within one working turn of A/peer criteria arriving.

## Owner reconciliation and precise remaining design gates

A upfront criteria on f666 are binding, not design acceptance. The inspected
runtime_bridge blob is `6d7637cdfac55b0803994c1e9290720d589f3c9a`.
fix237's full `reports/fix237-937-TP02-boundary-assumptions.md` is read and
aligned:933 does not edit runtime_bridge; TP-N3 owns pending-start changes,
including both queue_user and wait_for_start, native late-ID semantics and
route(c) exact no-write restore. Exact post933 source is still a captain input.
fix3 confirms there is NO competing TP02 production claim; its288/b93/47d9
S02/S03/S08/U10 work supplies conditions, not a second implementation.

Gate/cleanup columns shared by the method matrix above: only dispatch,
dispatch_queued,recover_queued,steer take the current sealed503 pre-validation
gate. All side-effecting methods, including non-gated goal/compact/explanation,
get a pre-lookup operation reservation and finally retirement or explicit
successor transfer; read/projection methods use their actual hub/snapshot
owners. begin_stop transfers to a lease; finish/expiry transfer lease to
completion then deferred children. release_agent owns deletion until its exact
resource outcome; compact transfers to the already-published compaction worker;
quota/recovery retain producer/recovery ownership and row admissions individually.
This accounting is NOT a new deny policy for formerly permitted calls.

Actual non-RPC producer census on f666:

| Existing producer | Publication/effect boundary and proposed accounting |
|---|---|
| turn_handoff._finish_slot -> deferred _spawn_next/_resume_and_spawn_claimed | pop_next owns the next exact trace before after-release spawn; retain existing parent/slot ownership through registering the deferred child, never enter coordinator under TURN_LOCK |
| turn_handoff._retry_handoff | pop_next under slot lock, then _resume_and_spawn outside; no hidden unregistered launch gap |
| turn_retry._schedule_retry/_retry_spawn | Existing retry scheduler retains exact spec/attempt; callback checks owner before _spawn_attempt; parent-to-child publication must be counted |
| turn_launcher._resume_after_account_switch | Existing account recovery/spec owner remains until admitted effect or held queue successor; no coordinator while failover lock held |
| turn_drain.start_held_work -> _start_ownerless_queues | Fence lowered first; actual child launch outside old admission lock; new design retains owner across this boundary |
| runtime_bridge._handoff_after_failed_stop -> _retry_handoff | Direct service reentry, not an RPC; no callback-to-global-lock inversion or forgotten child |
| runtime_bridge._expire_stop_lease -> complete_stop | Existing timer outside outer RPC gate, lease pop is not completion; one visible completion claimant through handoff |
| turn_recovery.recover_queued/_recover_queued_locked | Existing RECOVERY_LOCK,933 SQL and per-row submit fanout; no SQL/provider work under runtime metadata/status gate |
| compaction.compact_session -> _run | Existing _active publication before worker start is the actual successor; do not retire the parent before publication |

This is the known owner census, not a claim that an RPC-only counter covers every
direct provider producer. Re-run the source census on actual postTPN3 base before
code; A must accept the complete source-to-owner mapping. Preserve the atomic
`turn_stop.begin_stop` queue-state READ under TURN_LOCK while its pause WRITE is
deferred. Do not relocate that read under the slogan 'no DB under locks'.

The two idle entry paths differ today: begin_drain_if_idle can set hard
_draining without raising the soft slot fence, whereas seal_now raises it and
may lower it again. Do not silently normalize them to one new state sequence.
Existing RLock recursion seal_now->seal_if_drained must become explicit drain
operation/context reuse; never wait on that operation's own reservation.

**Unresolved before implementation, not granted by this note:**
1. Exact admission-phase hooks and existing outcome mapping. A runtime-only
whole-RPC lock replacement cannot prove same-agent Stop and canonical routing.
2. Exact Stop generation/payload/completion API. Preserve the owner conditions
without smuggling lead642's separately signed ordering refinement into a
latency-only patch. If source-preserving handling cannot satisfy the criteria,
request its explicit scope/signature rather than weaken them.
3. Concrete private pending-seal implementation and producer accounting. Lead937
approves the internal result and explicitly retains the four-method whitelist;
non-gated goal/compact stay permitted with accounting. Prove current drain safety
does not regress; do not make a full all-method post-seal no-launch claim.

A design-ready decision will therefore distinguish the latency/concurrency fix
from any separate Stop/release policy amendment. No new lock-across-provider,
SQL transaction, schema/state value, timeout or public blocker field is approved.
Next action is A's review of this exact first note and the bounded open choices;
no production/tests have been authored or executed.

## Lead937 policy rulings — incorporated after first draft

Scope is latency/concurrency ONLY: unrelated status/agents progress, same-agent
admission order/client-ID dedup/exact ownership/Stop payload conservation remain,
and drain safety does not regress. Four-method hard-seal set STAYS.636 is OUT to
backlog45. Today's shipped Stop semantics STAY;642 is not a rider. If A finds a
necessary semantic change, send the exact small intended-difference memo for lead
decision rather than expanding scope inside an internal helper.

Private PENDING/OWNED/SEALED between bridge and monitor, plus narrow named
admission/deferred-owner hooks, are approved INTERNAL seams only; concrete A
acceptance and owner agreement still required. fix237 owns TP-N3 regions; fix3
owns TurnSlots. Re-pin and land after933+BACKENDS-01/TPN3. A status-only early
mitigation must be independently revertible and explicitly PARTIAL; it cannot
close the high finding. All public shapes/codes/timeouts/wire remain unchanged.

Parallel TurnSlots diagnostic maintenance is not a TP-02 production claim: on
0073254687d2 the relevant fact helpers are module `_terminal_live(agent_id)`
(wrapping `terminal_ws.has_live_terminal`) and `_compacting(session)`. S4 has NO
production edits/hook claim in those functions or elsewhere in TurnSlots. An
exception binding plus bounded code/class-only throttled diagnostic retaining
False fallback and associated two-to-zero ratchet update is outside TP-02's
proposed seams; its owner/review controls still apply. Refresh source baseline on
actual landing rather than restoring an older TurnSlots file.

## A first-design review and API supplement

A report reviews/tp02-937-A-design-5ead1becd1f0.md FULLREAD: direction
accepted for refinement, F1/F2 changes required before implementation. See
`tp02-rpc-api-937.md` for exact proposed carriers/outcomes/monitor contracts and
a conditional canonical-binding/target-lifetime solution. In case of conflict,
its conditional F1/F2 wording replaces the first draft's unbound-cutoff sketch
for REVIEW ONLY; that semantic interpretation still needs A+fix3 settlement.
The actual post933/TP-N3 home/owner and preserving Stop adapter remain gates.
No production/test edits or execution. Explanation command inventory corrected
to local cache answer/redaction, no native RPC.

933's separately assigned narrow transfer_recovery_parks two UPDATE_HANDOFF
existing-marker payload edits do not conflict with this design census. Re-pin
that exact producer after933; do not edit those payloads or borrow A2/B's
precedence review as a TP-02 source grant.939 settled-trace selection and933
retry evidence remain with their owners.
