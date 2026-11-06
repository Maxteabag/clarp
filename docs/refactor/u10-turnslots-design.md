# U10: TurnSlots commands and explicit service dependencies

DESIGN ONLY under lead 678. Reviewer A2 rf-review-core2c-d509. No U10 code is
authorized. Base integration a733101781144cbfaf5e6dcdc3386cd71f445be0; discovery
989779c0 and report SHA256 f543f56b9890b004b171fd4306d808c4a229857c98fbd4b41e0f25828ddb5591.
Must reconcile with TP-02 design 288e377fb8c2112f204600c214a496efd037b575,
its A2 HOLD conditions and rf-fix3c-7498 BEFORE any implementation proposal.
TP-N4 remains parked and supplies neither code nor acceptance evidence.

## Fix and three obligations

Make TurnSlots the owner of state transitions rather than exporting writable
maps. Reuse the current object and OwnershipLock; no second lifecycle system.
Inventory every read/write of inflight, queued, claimed_at, launches, admitting
and fenced, including aliases through the facade, mixins, Stop and tests.
Convert production consumers by operation, not by moving arbitrary statements.

| Discovery obligation | Proposed boundary |
| --- | --- |
| U10.1 _INFLIGHT/_QUEUED/_CLAIMED_AT expose representation | Exact-owner claim/release, queue edits, Stop snapshot/restore and fence commands live on TurnSlots. Readers receive detached snapshots/typed observations, not mutable map handles. |
| U10.2 Seven mixins rely on an implicit constructor host | An explicit turn service context names slots, backend access, clock/IDs, scheduler and effect collaborators. Each operation declares what it needs; do not invent a generic service locator or a second mutable state cache. |
| U10.3 Lock order and deferred effects are caller conventions | Bounded commands make ownership decisions under the existing short lock, and return immutable decisions or enqueue owned effects through the existing defer path. Provider/DB/event work stays outside the outermost release. |

Keep historical imports while their actual callers migrate. Mutable aliases
cannot simply become detached dicts: writes would silently stop affecting
ownership. First migrate every production writer to commands, retaining the
same underlying maps for remaining compatibility clients; then decide with A2
whether residual mutation compatibility needs a narrow deprecated adapter.
Do not claim encapsulation complete while production writers bypass commands.
Tests migrate to real commands/fixtures, without adding production-only test
seams. Removal of any compatibility name is a separately audited final step.

Commands must express expected trace and existing generation/sentinel checks,
queue conservation and their current failure results; no generic set/update
API that merely renames writable maps. Provisional command vocabulary:
observe-owner, claim/release-if-owned, enqueue/take/restore, begin/restore/finish
Stop, mark-launching, and fence observation/transition. Verify which are already
correctly provided by TurnSlots before adding anything. Snapshot copies need
explicit payload-identity policy: queue entries include TurnSpec and context;
shallow/deep copying or freezing them must not change identity or callback use.

## Reconciliation with TP-02

U10 preserves current behavior. TP-02 owns the replacement of whole-RPC
serialization, canonical-agent admission lanes, unbound operation tickets,
release-generation checks, lease completion claims and Stop payload chains.
Its sealed-method and overlapping-Stop ordering refinements need their own
exact intended-difference signatures; U10 cannot absorb them as cleanup.

Current nesting is OwnershipLock -> account-failover coordinator. TP-02's
proposed coordinator -> short OwnershipLock -> lease-map mutex must compose
with that rule: never enter the TP-02 coordinator while holding a turn,
account-failover, backend/pool or SQLite lock; observations/callbacks cross
out through existing defer/explicit post-release calls. Never run provider,
filesystem, DB or fanout effects under either short decision mutex.

TP-02 tickets stay visible through a deferred child handoff: publication must
transfer the parent obligation before retirement. U10 can expose the exact
slot decision/owner observation; it must not treat absence of a map entry or
lease dictionary entry as proof that an effect completed. Decide with TP-02
owner whether the effect record carries a supplied opaque operation context,
without making TurnSlots import RPC transport or the admission coordinator.
Existing lease deadlines, original intent sequence, rollback ownership and
Stop payload conservation remain TP-02's vocabulary, not fresh U10 generations.
Reconciliation request has been sent; agreement is PENDING, not inferred.

## Frozen-surface check and change rehearsal

Preserve sentinel strings, FIFO and paused/bypass rights, exact-owner release,
spawn/admitting observations, queue capacity/failures, durable rehydration,
Stop receipts and status shapes. Preserve OwnershipLock reentry and deferred
callback ordering/error propagation. No new durable state, timeout, retry,
provider start, early thinking write, public event/route/CLI/env/path change,
release policy or expanded lock scope. No concurrency improvement is claimed.

Rehearsal: change the internal representation of claim timestamps from a
separate map to an existing-owner record in an isolated branch. Only TurnSlots
changes policy/representation; seven mixins and Stop continue to use the same
commands and detached observations. Trace release, stale/orphan repair and
status still have identical outputs. If multiple mixins need editing for that
change, U10.1/.2 is not solved. This is a design acceptance rehearsal only,
not authority to ship that representation change.

Ask A2 for cases before code: old-owner completion after successor claim,
terminal/STOPPING sentinels, queue arrivals during Stop/rollback, conditional
restore, drain/carry conservation, claim/launch/admitting lifetime, mutable
snapshot isolation, reentrant callbacks/defer errors and account recovery lock
ordering. Use controlled boundaries and reverse-order controls, not sleeps.
TP-02 adds its separate admission/seal/overlap matrix; no test substitution.

Re-run accepted discovery scripts on any eventual implementation into owned
outputs, with exact source/script hashes: production map writes/aliases, mixin
host dependencies, lock-order/effect sites, import backedges and special-case
sites. Record all three obligations separately; fewer aliases alone cannot
prove effect conservation. Runtime gates and own-base replay would be required.
No tests or discovery re-execution occurred for this design document.

## Decision state and route

Await A2 acceptance and explicit TP-02 owner reconciliation. U10 remains
DESIGN-ONLY even if U09 is accepted. Nothing lands before big merge #1. Any
future code permission follows that merge, from then-current main in small
captain batches, and needs fresh scope/source inventory. S4's approved M17
stage-one recipe/G0 packet remains separate and post-Friday-SHIPPED-held.
