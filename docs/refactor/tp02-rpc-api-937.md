# TP-02 / 937: bounded API supplement before code

DOCS ONLY, provisional f666fb0db240513ae6dae1eda8227046b4001088.
Responds to A reviews/tp02-937-A-design-5ead1becd1f0.md, F1/F2. This is a
concrete proposed API for review, NOT agreement to change shipped Stop policy,
code authority, actual post933/TP-N3 home certification or execution permission.
Parent design5ead1bec retains lead937 four-method seal/Stop rulings.

## 1. F1: reservation order is not admission order

A global reservation is PRESENCE for drain, not an admission decision, an
agent's queue position, or permission to perform any agent effect. It records
original request identity and sequence without blocking later resolutions. It
never waits for older unbound reservations. The canonical binding/admission
phase is the first point at which an overlapping, previously unbound request
can reserve a place for a specific actual agent. Two spelling aliases share
that one canonical target's lane. Once a decision commits, later resolution
cannot retrospectively insert an earlier global reservation in front of it.

Concrete histories (all calls genuinely overlap):
- R1 reserves but stalls lookup. R2 binds B, commits its existing durable decision
  and publishes an exact slot/queue owner. If R1 later resolves B, it enters B's
  NEXT admission phase and observes R2's decision; it queues/steers/refuses under
  the existing owner policy. It cannot undo R2 or manufacture an earlier queue
  position because its reservation sequence is smaller.
- Same stalled R1, but later resolves A: R2/B is independent. R1 has no B lane
  position. B's decision and response never wait for R1's lookup.
- R1 has already bound A and begun preparation; USER Stop A intervenes. Stop
  bypasses the ordinary preparation wait, publishes its existing durable/user
  and slot fence, and the old bound preparation must use its saved witness and
  existing owner/fenced outcome. Sampling the new generation as fresh is invalid.
- A causally later R2 submitted only after R1 returned must observe R1's durable
  outcome. This proposal changes no completed-before-invoked ordering.

### Explicit review question, not a silently dropped condition

For an UNBOUND R1 with NO existing durable agent admission/receipt, suppose
Stop A completes BEFORE R1 ever binds A. Proposed canonical interpretation:
its first agent decision linearizes AFTER Stop, exactly as a fresh overlapping
admission can in an admissible serial history. Global reserve did not grant
an earlier A decision. Existing old durable receipt/queued work is NEVER made
fresh this way: its saved USER generation, producer identity, pause rights,
launch classification and933 fence remain authoritative regardless of lookup.

This replaces the first draft's speculative rule that every unbound reservation
preceding any later-resolved agent Stop is necessarily old intent. That rule
also appears in retained288's DESIGN-only conditions. S4 has explicitly asked A
and fix3 to settle this interpretation, rather than silently deleting it. This
proposal is conditional on their source/preservation assessment under lead937.
If A requires that stronger original-unbound intent rule to preserve shipped
semantics, this supplement DOES NOT claim F2 solved or permit implementation.
Return the exact necessity/authority boundary; do not add new refusals, an
unbounded cutoff cache, timeout-based expiry or a new durable field to hide it.
No new lead policy request is made unless A establishes a concrete necessity.

## 2. F2: target records owned by live bound work, no historical cutoff map

Conditional on section1's interpretation, initial reservation sequence is NOT
used to retain a Stop-cutoff history for unknown future targets. Delete the
oldest-unbound-watermark pruning algorithm from the proposed implementation.

One TargetRecord exists only for a currently referenced canonical target. Its
references are held by bound/preparing/effect operations, active control changes,
published Stop leases/completion claims and explicitly registered deferred
children. The record contains current lane owner/waiters and a process-local
control version, not a list of past Stops or a durable generation substitute.
When ALL those references end, remove the key immediately in the same metadata
transition. Unbound operations do not hold references to every possible target.
Every binding reads the EXISTING durable USER generation and exact current
identity from its owner before it may admit effects. A pending concurrent Stop
must invalidate/retry that witness before authorizing the decision; its exact
owner-write protocol, not a map snapshot, supplies authority.

Bound: with U unbound operations and K live bound/control/lease/child references,
metadata holds U+K operation records and at most K target records. Additional
waiter/child records are included in K; no completed target tombstones remain.
One stalled U does not retain N completed Stop keys across N agents, deletion
or resurrection. Existing durable USER counters/receipt history are not copied,
pruned or changed by this registry. A bound token pins its record by identity,
so removal/recreation cannot make its version comparison pass through ABA.

Finite control: park R1 before lookup; create/stop/complete/remove target records
A1..A1000 with no leases/effects left. The live target count returns to0 after
each completion while R1 remains present. Bind a new request to a previously
stopped/resurrected agent: read its actual durable generation, do not reconstruct
an old in-memory cutoff. Bind an existing old receipt: its saved generation
still fences it. Repeat R1->B/R1->A histories; never use new503, capacity refusal,
key eviction while referenced, disconnect or age to force the bound to hold.

The stronger unbound-original-intent rule would require distinguishing which
arbitrary targets stopped during R1's unresolved lifetime. A single oldest
watermark does not bound that information, and a domain-wide per-agent cache
with soft-deleted/resurrectable IDs is NOT presented as a fix. Source f666
agents.create_agent can reuse a soft-deleted agent_id; deletion alone is not a
safe necessary-fence GC argument. This is why the explicit section1 joint
interpretation is a PRE-CODE condition, not an implementation detail.

## 3. Exact proposed carriers and operations

Private process-local types only; no RPC/DB/auth additions. Owner-issued objects,
not caller-self-equal dictionaries. All operations below are proposed signatures,
not added production exports. Finite states and outcome vocabulary stay internal.

| Operation | Inputs and result | Authority / failure contract |
|---|---|---|
| reserve_rpc(method) -> RpcOp | Existing RPC method class; initial sequence, phase RESERVED, no agent | Under short C gate; apply only current four-method draining refusal BEFORE validation. Others remain allowed with accounting. No lookup/I/O. |
| resolve_candidate(op, existing owner route) -> Candidate | Current command/row/run/session resolution; exact agent/resource and binding evidence | Existing owner lookup OUTSIDE C, no side effects granted. Validation retains existing response/exception precedence. |
| pin_target(op, canonical_key) -> TargetRef | Pin record identity/version, no admission position yet | C-only ref increment; no metadata lock held over validation/wait. Not an agent identity check. |
| bind_decision(op, target_ref, OwnerWitness) -> Bound / ReResolve / OwnerRefusal | Witness is from canonical owner's existing validation/USER generation/receipt state | First agent admission order, not initial global sequence. ReResolve before effects only; no replay of SQL/native effects. OwnerRefusal maps existing owner outcome; no new code/reason. |
| commit_decision(bound, OwnerDecision) -> DecisionOwner | Actual owner commits queue/admission/claim under its current boundary | This is NOT a metadata precheck followed by an unrelated write. Owner must validate receipt/Stop/slot authority AT its write/claim; decision publishes exact existing successor. |
| enter_effect(decision_owner, captured native/slot owner) -> EffectOwner | Retain admission operation before releasing decision phase | No C/agent mutex across RPC/DB waits. Existing native/slot protocol revalidates exact owner and covers late replies. C ticket remains visible across any publication gap. |
| transfer_to_child(parent, ChildOwner) -> ChildRef | Parent-issued child description/captured exact owner | Parent survives outermost owner-lock release; C registers child BEFORE publication/parent retirement. No callback into C under TURN/backend/failover. |
| complete(op, Outcome) | NoEffect / Completed / Transferred(existing owner) / Uncertain(existing execution owner) | Exactly once under C; cannot retire an uncertain possibly-running effect merely because caller disconnected or raised. Validate successor reference before releasing last parent ref. |

OwnerWitness and OwnerDecision are not fabricated by the coordinator. They bind
existing canonical identity, receipt/trace/queue and control/slot evidence; they
must be supplied by actual owner hooks. No `_SLOTS` dict is exposed to the bridge,
no new binding CAS or provider success proof, no new quiet retry on evidence error.

Error/outcome matrix: malformed input -> same existing400/404/409 path, operation
finally NoEffect; existing hard seal -> exact existing503 before method body;
queue/admission DB error -> existing DispatchError/outer500 path without replay;
backend timeout -> native owner uncertainty/hold, not unsent; invalidated prepared
owner -> existing receipt/slot refusal or durable queued result; known successful
native effect -> never release its owner because later bookkeeping/flush failed.
Concrete message/status mapping for each new hook MUST be pinned to the actual
post933/TP-N3 source before A can accept the adapter. This table is not permission
to invent a refusal at a previously non-refusing point.

## 4. Proposed hooks, actual source homes still to re-pin

| f666 home and hook location | Inputs/result and lock state |
|---|---|
| AdmissionMixin._dispatch_owned, turn_admission.py, after existing target/agent lookup BEFORE agent side effects | Candidate -> binding/witness and agent admission phase; no C held over lookup/DB. Existing route/dedup logic stays here. |
| Same method, actual durable admission/claim decision | Owner witness + transaction/receipt result -> OwnerDecision; C never acquired inside SQLite writer. Existing transaction authority validates its own generation, not a coordinator boolean. |
| TurnDispatchService._spawn_attempt_claimed, facade turn_dispatch.py | Exact spec/attempt + existing slot/native claim -> retained EffectOwner. Distinct from LauncherMixin._spawn_attempt_owned guard wrapper. No old raw facade copy. |
| Existing steering boundary (facade _steer_if_supported) | Exact admitted follow-up/active owner, receipt-before-steer preserved; native effect owner retained, no provider call under metadata lock. |
| HandoffMixin._finish_slot and after-release _spawn_next | Existing slot/pop parent -> deferred child. TURN owns decision; parent/current slot remains visible while child registration is deferred. |
| Retry/account/drain/recovery producers in original census | Existing exact spec/recovery/queue owner -> child, preserving backoff/FIFO/933/TP-N3/route(c). No unused injection seam or independent second admission engine. |

These are NOT granted edits. fix237 owns933/TP-N3 regions, fix3 owns TurnSlots;
936 lifecycle/INV1 separate. Actual parent object and register-before-publication
API must be proven at each actual postTP-N3 callsite, including nested defer and
synchronous callback. If a parent slot disappears before child registration and
no other owner survives, that callsite is BLOCKED: do not fix the proof with a
comment, an early ticket retirement or acquiring C under TURN.

Lock trace: reserve C -> release C -> resolve owner -> pin C/release -> existing
agent/DB decision boundary -> release that boundary -> register/transfer C ->
release -> effect. No C nested under TURN/slot/pool/claims/move/failover/lease/DB.
A reference to an existing parent is NOT itself new coordinator authority: the
seam must retain that parent's actual blocking lifetime until child publication.
No new synchronization is authorized by this trace; any proposed short nesting
needs exact owner/A review and a total order before code.

## 5. Stop and lease transfer contract (no642 policy)

BeginStop reserves an operation before barrier/pause/fence work. Its existing
owner commits USER-vs-maintenance semantics and exact target. Native interrupt
runs outside C/TURN/lease locks under that owner. On success publish the existing
lease under C -> lease-map short locking, transferring begin ownership to lease
atomically. Schedule ORIGINAL30s timer outside locks; no lease timer restart.
On timer-start failure, retain the operation/lease owner while the actual current
owner performs its preserving error cleanup; no lost barrier or invented success.

finish and expiry both execute claim_completion(lease_id): acquire C then lease
map, create completion owner FIRST, pop exact lease, release both; only winner
calls existing complete_stop outside locks. Loser RPC keeps404, expiry no-op.
Completion owns the saved snapshot through filtering/restoration/deferred child
publication; pop is not retire. Exception/partial effect cannot erase ownership
or retry native interruption. No lease-lock -> C/TURN edge. Disconnect/response
serialization failure does not cancel worker ownership.

Reentry: an internal call inherits an explicitly issued parent reference, never
an RLock recursion exemption. seal_now calling seal_if_drained reuses its own
drain operation and cannot wait on itself. Callback invoked under a domain lock
uses the retained parent after OUTERMOST release, never calls/waits on C there.

Shipped overlapping Stop semantics stay,642 refinement OUT. Concrete S1/S2
barrier/rollback/payload transfer adapter is STILL a pre-code proof obligation,
not granted by these type names. A must reject any proposed adapter that releases
a newer barrier, drops saved work, extends an old deadline, or quietly creates642
readiness waiting. If preserving correctness requires that semantic change, only
then send lead the precise necessity memo. No repark/queue rollback writer;36 open.

## 6. Private seal contract and monitor calls

SealAttempt returns exactly one of:
- PENDING: admitted operation/lease completion/child publication or dirty epoch
  prevents a final decision; NEVER permission to hand over. No synthetic agent
  blocker/public field. Monitor uses its existing later tick, budget and phase.
- OWNED(blockers): actual current blockers, preserving current dictionaries and
  phase behavior. Not a fabricated in-flight-RPC ID posing as a provider turn.
- SEALED: exact current phase/epoch and short owner facts validated, no unbound or
  effect/lease completion gap; current allowed post-seal methods remain allowed.
  This is current drain nonregression, not backlog45's stronger no-new-native policy.

Every existing monitor caller is adapted explicitly: _natural's
begin_drain_if_idle path, default check_once fast-idle, _natural seal_now, and
soft-drain seal_if_drained. Only SEALED calls _hand_over; PENDING cannot be
converted into{} by a legacy return-shape adapter. OWNED retains current blocker
reporting. No new public status key, hard-seal refusal or monitor busy loop.

Natural fast-idle can hard-seal without raising the soft fence as today. Explicit
seal_now raises/later-lowers the soft fence as today. Persistence/status/carry
scan stays outside C, retained under a drain operation. Final C commit validates
same epoch and no invisible owner while excluding ONLY that same drain attempt
from its own count. Cancel/revert increments epoch and invalidates old scan;
release_admission_fence lowers before start_held_work outside lock with parent
ownership bridging to each eligible child. Carry error cannot yield SEALED.

Final facts cannot be snapshots silently read before a conflicting owner change.
The exact publication/epoch handshake with each actual owner is still subject to
A/file-owner acceptance. Conservative PENDING/missed seal is allowed; cutting an
uncertain live owner is not. The four-method sealed set stays exactly unchanged.

## 7. Independent bounded controls and proof limits

No tests authored/run here. Captain alone executes authorized RED/GREEN/gates.
Add distinct controls to existing owner fixtures, not table-self oracle:
- Stalled-unbound R1 -> B and -> A, overlappingR2 commitsB first; no global wait or
  retroactive reorder; original same-ID/queue/steer/durable outcomes checked.
- Bound-before-Stop vs unbound-before-Stop histories separately, actual queue and
  existing USER generations; old durable receipt always fenced. Canonical-first
  interpretation itself awaits joint review before either is an expected test.
- Stalled lookup plus1000 target stop/create/delete/resurrection cycles: live-only
  target state returns to0 and later binding honors durable receipt/generation.
- Parent decision -> nested defer -> child published; seal at every gap, exception
  and client disconnect. Native A held while B/status/Stop progress; no own-child
  wait and no callback-to-coordinator inversion.
- finish/expiry winners, popped lease+blocked completion vs drain, timer-start
  failure, overlappingStop/rollback with existing original deadline/404/no642.
- Four monitor entry paths PENDING/OWNED/SEALED, dirty epoch/cancel/carry failure;
  exact wire/error matrix and existing natural fast-idle behavior unchanged.
- Mutants restore whole-RPC serialization, retire before effect/publication,
  ignore popped completion owner, turn PENDING into{}, or drop a referenced target.

A's first review architecture direction is accepted, not implementation. F1/F2
are offered a concrete conditional resolution here; the changed unbound-intent
interpretation is explicitly NOT presumed agreed. Actual post933/TP-N3 pin,
canonical outcome matrix, owner hooks/transfer handshake and preserving Stop
adapter remain exact pre-code gates. No generic source/atomicity/runtime grant.
