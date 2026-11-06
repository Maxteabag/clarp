# Step 0: shared declarations and owner-executed lifecycle writes

DESIGN PROPOSAL, lead696; A2 policy and B infrastructure review required.
Baseline a733101781144cbfaf5e6dcdc3386cd71f445be0, lifecycle.py blob
1addf38853070645455a6927f058768f2b52ecae. No implementation, tests or jobs.
M17 repair edfb0e65 has B/A3 source approval; captain/postship gates are separate.

Read explicit-lifecycles-plan.md, catalogue b129f440, S02/S03/S08 and S42,
turn_lifecycle.py, both Step0 reviewer criteria, and the contract owner's
s4-step0-api-inventory.json. Recheck the actual post-big-merge main before code.
No family conversion or claim that a table alone closes a catalogue obligation.

## One engine and additive API

Extend lib/lifecycle.py; do not create another actor engine or state cache.
Factor the existing pure event/source lookup for reuse by both declaration
front ends. Keep Lifecycle's constructor, validation, exceptions and methods
compatible. Its SQL-specific/final-state rules remain where they are. New
explicit-vocabulary validation is opt-in through Rules; no incidental tightening
of old Lifecycle declarations or automatic owner migration.

Proposed public signatures (types below are contracts, not implemented code):

    Rules(*, family: str, owner: str, states: frozenset[str],
          events: frozenset[str], initial: str, final: frozenset[str],
          edges: tuple[Edge, ...], waits: Mapping[str, NamedWait],
          refusal_codes: frozenset[str], transient: frozenset[str] = frozenset(), cyclic: bool = False)
    DecisionPolicy.decide(state: str | None, event: str) -> str
    Rules.decide(state: str | None, event: str) -> str  # implements DecisionPolicy
    NamedWait(*, name: str, exits: tuple[WaitExit, ...], reason: str = "")
    WaitExit(kind: ExitKind, name: str, deadline_source: str = "")
    OwnedEvent[E, P](name: str, entity_id: str, expected: E, payload: P)
    OwnerWritePort[E, P, T].write(policy: DecisionPolicy, event: OwnedEvent[E, P],
                               reject: RejectAtWrite) -> Outcome[T]
    OwnerWritePort.after_boundary(report: Callable[[], None]) -> None
    apply_owned(policy: DecisionPolicy, event: OwnedEvent[E, P],
                owner: OwnerWritePort[E, P, T], refusals: RefusalSink) -> Outcome[T]
    RejectAtWrite(reason: RefusalCode, state: str, ids: SafeIds,
                  *, primary: BaseException | None = None) -> Refused
    RefusalSink.capture(family: str, event: str, state: str,
                        reason: RefusalCode, ids: SafeIds) -> RefusalNotice
    RefusalSink.emit(notice: RefusalNotice) -> None

DecisionPolicy is the pure lookup protocol, not an alternate engine. Rules
implements it for NEW declarations. The first legacy S02 port binds an
owner-private view of the ACTUAL machine table/is_legal policy and preserves
its existing target-lookup timing and exceptions. It does not construct stricter
Rules from a default table, cache a second policy or impose new final noops.
This compatibility view is not a public force/bypass option for new declarations.

Rules snapshots small declaration metadata at construction: copied immutable
state/event/code sets, edges and wait mappings, and frozen NamedWait/WaitExit
structural values. Caller mutation cannot invalidate a validated declaration.
This is not a copy of borrowed P or new validation of legacy Lifecycle.

Outcome is exactly Applied(value), NoOp(value), Refused(code, observed_state)
or WriteConflict(code). The existing Conflict exception is NOT shadowed. Unexpected writer/storage errors propagate unchanged; they
are not reclassified as harmless refusal. Applied means the named owner
mutation completed, NOT that every downstream effect or store binding is safe.
Outcome kind is never inferred from value truthiness: Applied(None), false or0
can be legitimate results; no-target, idempotent noop and conflict stay distinct.

ExitKind is the finite set event/deadline/user/refused/error/closed/superseded;
each exit has its owner-declared stable name, and only deadline exits name an
existing deadline_source. RefusalCode is a string from Rules.refusal_codes,
or the finite inventoried legacy owner reason set for its compatibility view,
never arbitrary payload text. SafeIds has only entity_id, trace_id, operation_id
and generation scalar-id slots, with bounded diagnostic representation; it
cannot contain an opaque proof object or payload. Unknown diagnostic values
use fixed tags rather than unbounded counter labels. Domain guards retain
their exact old reasons through owner translation.

E is an owner-defined immutable expectation: captured scalars or an opaque
issued reference plus separately captured phase/target facts. P is borrowed
synchronous payload, never a generic deepcopy or diagnostic value. The kit
does not infer identity from event names, truthiness, stringification, newest
pool state or caller-created equal records. It issues no wire permission.
There is no generic force flag; existing repair/compatibility entrypoints stay
with their owner and are individually inventoried.

Rules validates finite vocabulary, unique event/source targets, known states,
initial/final/correction rules, reachability and named exits. Ordinary self-loops
are real writes; explicit final corrections already at target are noops as in
the existing kit. A cyclic owner dimension explicitly uses cyclic=True and may
have no final state; finite per-operation outcomes use cyclic=False. Do not
invent a persisted final state to squeeze an ongoing resource into a row model.
Every nonfinal state is either declared transient with an immediate owner
continuation, or has a NamedWait. A wait names actual event/user/deadline/error/
closed/superseded exits. Deadlines name an existing owner field/timer; the kit
creates no timeout, expiry task, busy wait or implicit WaitForever expiry.

## The hook is the entire atomic owner operation

write is ONE owner-executed hook. It reads authoritative state and actual owner
identity, checks event.expected, decides legality and writes within the SAME
existing lock, SQLite write transaction/CAS, or original reader-owned operation.
There is no public unlocked precheck followed by a separate writer callback.
The kit cannot certify an arbitrary implementation of this protocol atomic:
each concrete port requires source review and independent at-write barriers.

The owner projects state from existing rows/maps/fields; Rules holds policy
only. No second mutable enum can disagree with the authority. A zero-row CAS
is WriteConflict in the new outcome API (legacy Conflict stays unchanged), an illegal pair is Refused, and writer failure is still writer
failure. The owner maps outcomes to its existing public returns/exceptions.

reject is the attempt's diagnostic-only helper, not a guard or writer. The
owner invokes it AFTER rejecting the actual at-write facts. It captures one
bounded notice/count for this attempt without logging, SQL or external effects;
apply_owned arranges emit through after_boundary. Re-entering reject in one
attempt does not double count; a distinct attempted event gets its own notice.
The owner supplies the existing synchronization for capture/snapshot/reset:
the turn machine's existing diagnostic lock, a memory owner's existing lock,
or an exclusive original reader context. No mutex is constructed by the kit.
A logger/DB-writing callback is not permitted in capture. If the selected
owner cannot supply that boundary, its port stays unapproved, not unlocked.

### Attempt lifetime and exceptional completion (D1 amendment)

The initial new API supports ONLY synchronous, wholly owned write boundaries.
Each apply_owned call receives a NEW attempt-local port facade, constructed by
its owner at that callsite. It references the shared owner but is never reused,
shared between threads, or stored as the owner's singleton port. Nested calls
need their own facade and closure; the underlying owner must separately permit
that ingress. A fresh facade does not authorize S42 nested MIRROR admission.
The single-use marker belongs to this short-lived orchestration object, not a
second entity state/generation or an issued-operation registry. No new lock.

write enters and leaves its own existing protected spans through structured
with/try/finally on BOTH return and raise. after_boundary(report) is a synchronous
attempt-local completion method, called only after write has unwound those
spans. It immediately invokes report once in the same stack; it does NOT register
or retain a callback for a future commit, search for the current transaction,
or infer release by probing a lock. Source review must prove every write exit
has unwound the relevant owned boundary. S02's observation also remains in its
existing post-COMMIT/release position; S42's completion is still before D.

Caller-owned/joined outer boundaries are UNSUPPORTED by this initial new API,
even if they have a future completion mechanism. A later adapter needs a separately
reviewed attempt-bound existing completion hook and failure contract. Old SQL
CAS joining APIs remain unchanged. In particular S02 rejects an already-open
transaction BEFORE SQL, capture, or completion work; that path never calls reject
or after_boundary and cannot log while a borrowed SQLite writer is held.

Control-flow sketch (design only; local variables live for this one call):

    notice = None; emission_started = False
    # reject_once captures at most one notice under the owner's existing
    # diagnostic synchronization. It never emits or schedules anything.
    def emit_once():
        if emission_started: raise internal_contract_error
        emission_started = True  # BEFORE invoking a possibly failing reporter
        refusals.emit(notice)
    try:
        result = owner.write(policy, event, reject_once)
    except BaseException as primary:
        if notice is not None:
            try:
                owner.after_boundary(emit_once)
            except BaseException as diagnostic_error:
                raise SAME_PRIMARY_WITH_ORIGINAL_TRACEBACK from diagnostic_error
        raise
    else:
        if notice is not None:
            owner.after_boundary(emit_once)
        return result

SAME_PRIMARY means the original exception OBJECT/class/reason with its original
traceback retained, not a replacement, ExceptionGroup, benign result or retry.
If a reporter itself raises that same object, re-raise it without self-causation.
The reporter error remains an explicit chained cause when distinct; it is not
silently swallowed and triggers no automatic second log/emission attempt. With
no primary failure, a completion/emitter exception propagates to the existing
owner error boundary; it does not return Refused/Applied or rerun W. A previously
committed write stays committed, including a post-COMMIT observer exception.
No new retry classification is introduced by this diagnostic wrapper.

On an exceptional domain refusal the owner constructs the ORIGINAL exception
before calling reject_once(..., primary=that_exception), then raises that same
object. If capture itself fails, reject_once raises the supplied primary from
that capture error; without a primary, capture failure propagates. A failed
capture is not retried. Thus capture/completion failure cannot replace the
primary exception. A normal data Refused may capture without primary and has
the explicit no-primary reporter-failure behavior above. An unrelated writer
error with no captured notice causes no completion call or invented refusal.

No callback registration occurs inside write or on its exceptional path: this
is deliberately the narrow synchronous completion mechanism, not a new engine.
The concrete port must call its completion callback immediately, exactly once,
without effects before it; omission/double invocation is an adapter contract
failure covered by independent controls. No callback survives apply_owned.
Two interleaved/reentrant attempts cannot exchange notices because neither the
port facade nor the notice/emission closure is shared. A stale S42 attempt may
refuse its own replay; it must not retire/clear the legitimate or replacement
issued slot. Only the admitting original dispatch owns that retirement.

Legacy transaction/Outbox remain unchanged: BEGIN IMMEDIATE rejects nesting,
rollback drops external effects, COMMIT precedes them, independent effects run
in order even after one fails, and EffectsFailed leaves committed state intact.
Inline readers/RETURNING materialize before return; they are not external effects.
New ports express dependent continuations explicitly, never flatten them into
independent Outbox entries. No durable/exactly-once outbox promise is added.

## First concrete consumer proposal: S02 conditional opening end

Owner rf-fix3c agreed 719c5a48 to S02 as the first production port, conditional
on exact API acceptance; S4 supplies the kit, not a competing turn conversion. Exact reference:
turn_lifecycle.TurnStateMachine._conditional_transition, a733, also unchanged
on Friday a89. Current caller is turn_retry._end_opening_state.

    CallOpenFence(expect_trace: str, anchor: int | None, s_open: int | None)

These values are captured by the existing caller BEFORE its ownership check;
never refresh them after waiting. Under the EXISTING _critical_write and
BEGIN IMMEDIATE, the port retains these checks immediately before INSERT:
MAX(state_log.state_id)==anchor (including NULL/None equality, no coercion);
traces.trace_id==expect_trace; that turn's
settled_at IS NULL; s_open exists; no matching prompt-admission state row after
s_open; the current kind still permits the event. This is generation + trace
fencing under the existing SQLite writer transaction, not a state-only CAS
misrepresented as a stronger guard. No new BEGIN, lock or SQL predicate.

The authoritative state_log INSERT, existing _settle_turn call, write_lost
check, COMMIT, live_hub observation and return retain their current sequence.
Do NOT strengthen the existing settlement/derived-error limitations. Borrowed
open transaction refusal remains the current pre-admission in_transaction path,
before any statement; this first port does not invent transaction joining.
Known write_busy/write_lost/g_anchor/p1_trace/p2_settled/p4_no_admission_mark/
p4s_prompt_admitted map to the existing TransitionNotWritten and caller False.
The pre-admission in_transaction path retains its exact current exception/code
and performs no diagnostic state lookup. Illegal edges map to the existing
IllegalTransition. Other SQLite errors remain
errors. Use the actual instance/custom table and the caller's existing selected
event; preserve target lookup/error placement before the transaction. No new
final-state noop, permitted-origin tightening or missing-fact normalization.
_settle_turn's derived restamp/receipt calls stay inside this operation with their
current swallowed-failure limits; write_lost still detects a lost transaction.
Only live_hub observation follows COMMIT AND _critical_write release; an observer
error propagates after the committed row, with no rollback/refusal/replay.

The existing machine illegalTransition diagnostic and caller callOpenEndRefused
are DISTINCT records and both remain. Split the machine's existing illegal
capture/count from emission after rollback/release; do not add a third generic
kit log. g_anchor/write_busy/write_lost and other guard refusals must NOT inflate
legacy illegal_counts. A new finite generic counter, if used, is a distinct
metric, not a relabeling or duplicate of that existing illegal counter. Preserve
the caller's write_busy/write_lost versus callOpenEndRefused log selection.
Exact owner agreement 719c5a48 is satisfied conditionally on the amended API;
no repeat ownership request is needed. Diagnostic exception flow is D1 above.

This narrow first port is NOT full S02 conversion or a new outcome guarantee.
Reuse existing callopen r6/P4s boundary controls: successor after check, Stop
before write, same-trace newer row/ABA, wrong live trace, missing admission mark,
new hook admission and predecessor-before-anchor valid positive. A removed
anchor/trace/write-time-legality guard must fail those real histories.

## TP-02 / U10 adapter constraints

Owner b93b28bb/47d9e822 and 288e377f retain tickets/USER cutoffs/Stop-chain/seal.
Rules declares independent slot, Stop payload, queue pause and hard/soft release
dimensions; no single enum replaces them. In particular begin_drain_if_idle's
current hard-draining/soft-unfenced state is not normalized away.

Memory ports use the existing OwnershipLock and exact captured owner facts.
Stop's queue.state READ stays under that lock; its pause WRITE remains deferred.
Parent ownership exists before the turn decision, survives defer out of any
held backend/failover/turn lock, and transfers to a child BEFORE publication and
parent retirement. No coordinator entry under those locks or SQLite writer.
S1 data survives an S2 barrier: Q0/Q1/Q2, cancellation cleanup and original30s
deadline stay with the Stop-chain owner; map absence is not completion.
No U10 code or new TP-02 policy is authorized by the kit.

## S42 adapter example: b3ccc863 contract, not binding CAS

The RPC/notification owner issues a bounded operation reference at the actual R
admission, owns its phase/target slot and retires it in that dispatch's finally.
A retained reference cannot replay after slot retirement; two identical separately
received notifications still receive distinct operations. No global history.

Proposed concrete memory storage is ONE active mirror-operation slot on the
original client, issued from the actual response-pop/notification R adapter.
A frozen MirrorFence carries original_client_ref, original_reader_ref,
issued_operation_ref, expected_phase and the owner-associated scalar target.
The slot holds phase and the admitted identity; it is not the authoritative
goal row, pending-request map, latest-pool entry or a retained operation history.

Proposed enforcement uses the EXISTING original-client lock for short slot
issue/check/phase mutation only: require slot.reference IS the issued reference,
matching original routing and associated target, and TARGET_RESOLVED; change
phase to STORE_ISSUED once, then release BEFORE W. S6 must confirm this exact
lock/context at the composed base: R already releases the client lock before
the callback and requesters must not hold it across done.wait. Notification
issuance must remain in its original reader order. If a real reentry path
cannot use one bounded slot safely, resolve that before code, not with a history
set, another lock or a new retirement hold. This is proposed, not enforced proof.

The write port checks issued-reference identity, captured ORIGINAL client/
reader/routing facts, current operation phase and its associated L target.
R facts are compared to the retained issued record, not a fresh pending-map
lookup: R has already popped that pending entry. A fake record with matching
fields is not the issued reference. Retirement clears the slot and borrowed
payload/result references in the existing finally path.
It does NOT check the newest pool client or revoke on requester timeout.
Actual slot storage/issuance/reentry must be agreed with S6 on the composed base;
caller-self-equal records are not implementation of this port.

| State/event | Mutation / existing continuation |
| --- | --- |
| ADMITTED / no target at L | retire locally; no W/K/P |
| ADMITTED / target found | TARGET_RESOLVED with fixed scalar target snapshot |
| TARGET_RESOLVED / valid issued W | STORE_ISSUED before ONE synchronous existing store attempt |
| STORE_ISSUED / store raises | existing unknown mark/log; retire WITHOUT P or retry, even if SQL committed before readback failed |
| STORE_ISSUED / returns | STORE_RETURNED, recorded=True, retain ACTUAL returned/readback row |
| STORE_RETURNED / K returns | MARK_HANDLED; newer write/unanswered mark may legitimately remain |
| STORE_RETURNED / K raises | existing post-store failure/retire, recorded=True; NO P |
| MARK_HANDLED / no stream | retire with no P |
| MARK_HANDLED / stream | issue P inline using resolved target + actual row |
| P returns/raises | retire; recorded=True stands; existing failure handling, no rollback/replay |

S42 K then P is a DEPENDENT continuation, not Outbox(K,P). Original reader
mirroring remains before the old done-finally; notifications do not acquire a
new response waiter. Retire payload references on every existing exit.
K returning normally with a falsey value still permits P. Clear returning
None/zero is Applied, not a zero-row CAS conflict. A slot must not be revoked by
_fail_all, shutdown, process death or timeout after admission. Source c786772a
supports the short existing non-RLock spans conditionally: capture M and release
the goal-state lock BEFORE client-lock association; release the client lock
BEFORE W/K/P/logging. Do not add reverse nested acquisition. Actual ingress must
capture the original reader; sampling a receiver's reader field at W is not proof.
Actual synchronous reentry remains HOLD, never a reason to drop a valid event.
Keep response versus raw-notification malformed-goal normalization distinct.
No arbitrary goal/JSON copy, new pool/claims/move lock, nested RPC, requester
wait, async reorder, timeout or pool-retirement hold. Borrowed JSON lives only
through the original synchronous callback; expectations retain scalar identity.

L-rebind-W remains the currently allowed write to L's resolved target. The
existing agent_goals write is by agent_id with no client/thread/binding CAS.
The kit protects this operation, not that absent cross-entity predicate.
S16 continuity remains OPEN; any stronger predicate is a separate intended fix.
S42 actual API/enforcement/base/barrier acceptance is still pending, not implied
by these signatures or the current reference contract.

## Diagnostics, helper and proof

RefusalSink has fixed finite family/event/state/reason keys; ids are values,
not unbounded counter keys. Unknown labels collapse to fixed diagnostic tags.
Use owner collaborators: no logger/DB import in the pure decision engine, new
diagnostic transaction or new synchronization lock. Counters/recent records
are bounded; never record payload, goal, argv, SQL data, token or exception text.
One captured kit notice/emission attempt per attempted rejection; wrappers do
not count it twice. Existing distinct owner/caller diagnostic channels remain.
Preserve legacy observer information and failure mapping explicitly. Reporter
failure cannot turn refusal into success. No silent catch is added.

A helper under tests enumerates structural pairs, vocabulary and case registration.
It is NOT an oracle generated from the table. Independent actual-owner traces
and old expected outputs detect missing/extra edge, wrong target/wait exit,
missing at-write fence, ABA/wrong token, Stop/close, zero-row CAS, writer failure,
early/double effects, RETURNING deferred,
interleaved attempt notices, exceptional rollback/release-before-emit, capture/
emitter failure retaining the same primary, no notice on unrelated failure,
and dependent K failure with no P.
S42 adds original callback after timeout/latest-pool change, same-operation replay
versus distinct notifications, no target, W/readback/K/P failures and L-rebind-W.
Keep the existing strongest SQL/callopen/N2 controls; do not duplicate full suites.

Rehearsal: in an unlanded edit add one owner-local event/guard/exit to its single
declaration and an independent expected trace through the real port; restore it.
Re-run owned catalogue/writer/backedge/special-case scans after conversion, with
exact source/script hashes. A new kit declaration is not a converted-family count.

## Compatibility and pending agreements

Contract-owner inventory pins every legacy SQL signature to a733/1addf388.
Keep create/transition/state-only and set CAS, target-only stamping, reserved
columns, inline fetchall, single-target autocommit, joined transactions and
multi-target ownership/restrictions exactly unchanged. No new binding guarantee.

Reaper3704d885/patch3cc50be4 is PAUSED/PENDING, not an approved default. Reserve
states/cutoffs/values_by_event/where/params and batch_budget_ms; Step0 does not
import them, change budgets/cursors or resume that benchmark. Keep its diff
separate. No namespace collision or new cleanup/expiry behavior.

Recorded settlements: B infrastructure direction ACCEPTED c8f29ae9; contract
namespace/legacy SQL overlap AGREED c8f29ae9; S02 first-owner agreement 719c5a48
conditionally satisfied; S42 reference composition c786772a supported by S6/A2.
These are not code grants. This amendment requires A2/B exact D1/API review and
contract overlap recheck for DecisionPolicy/completion changes. S6's actual
composed-base ingress/one-slot/reentry HOLD remains; source reference d68a is
not the implementation base. No implementation until those agreements settle.
All converter code/landing follows the authorized after-big-merge main route.
No frozen value, schema, route/payload, env/path/hook/CLI, timer or reader order
change. TP-N4 remains parked. Designs/acceptance cases are not executed proof.
