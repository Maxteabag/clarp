# U09: runtime values and method codecs

Design only, lead 678; reviewer A2 rf-review-core2c-d509. No implementation,
new tests, runtime execution or landing is authorized by this document.
Base: integration a733101781144cbfaf5e6dcdc3386cd71f445be0. Discovery:
989779c0bced90abe07bbd9ed41079c741d999bc, report SHA256
f543f56b9890b004b171fd4306d808c4a229857c98fbd4b41e0f25828ddb5591.
Recheck the actual post-big-merge main before implementation, including S4's
approved stage-one prep 0743d86b and Friday source ad454ee7. TP-N4 stays parked.

## Fix and three obligations

Create `lib/runtime_contract.py`, a standard-library-only value/codec owner
below both RPC and turn execution. It owns DispatchCommand, DispatchResult,
DispatchError, StopLease, protocol constants/errors, JSON framing values and
method descriptors/codecs. No sockets, locks, database, config, backend,
provider, scheduler or execution service imports belong in this leaf.

DispatchCommand currently embeds PromptAdmission and calls its parser. Moving
that import unchanged would leave the leaf reaching into a database-importing
module. Move only the PromptAdmission value class and its unchanged JSON
methods into the same contract owner; prompt_admissions retains creation,
authority, database work and compatibility re-export. This is value ownership,
not a change to admission authority or accepted provenance. Check imports of
ADMISSION_VERSION and preserve its one value through a compatibility export if
needed. Do not move unrelated admission policy.

| Discovery obligation | Result |
| --- | --- |
| U09.1 RuntimeClient imports the turn facade to construct/decode values | Client imports the contract; neither forming a command nor decoding a dispatch failure imports turn state. |
| U09.2 DispatchResult shares admission/database/turn-lock imports | Execution imports the same inert values. turn_command and turn_dispatch re-export the identical class objects while callers migrate. |
| U09.3 Client methods and server switch are independent catalogues | One contract descriptor per existing wire method owns name, parameter/result codecs and symbolic executor binding; client and server consume it. No separate server name switch or independently maintained codec list. |

Descriptors carry inert executor keys, not execution imports. RuntimeRPCServer
binds those keys to its explicit handler methods/collaborators once at
construction; a missing/duplicate binding is an internal construction failure,
not a new wire refusal. Compatibility RuntimeClient methods retain signatures
and choose descriptors rather than repeat string names and result parsing.
Each descriptor retains separate client/server conversion functions where
current policies differ; sharing a type does not license stricter validation.
No generic coercion should replace today's method-specific behavior.

The outer dispatch_request admission lock and existing drain check remain in
runtime_bridge at the SAME point before method-body validation. Keep the
Janitor interaction-capability prelude before the same five methods. Streaming
live_stream remains the transport bypass it is today. Unknown-method 404 and
the request handler's catch/log/500 envelope remain at their current boundary.
Authorization, release classification and TP-02 tickets are execution policy,
not descriptor defaults inferred from method names.

## What stays and frozen-surface check

TurnSpec, PromptAdmission creation/validation authority, goal/call-open gates,
receipts, retry/Stop logic, `_with_delivery`, state, queue ordering, handover
and effect ownership stay in their current services. DispatchError subclasses
stay with their policy unless a demonstrated value-only dependency requires
otherwise; they inherit the SAME moved DispatchError. RuntimeUnavailable and
RuntimeProtocolError retain their identity via runtime_bridge exports.
GoalUnsupported's existing 501 translation remains an execution-aware adapter;
the contract must not import backend implementations to construct it.

Preserve protocol version 1, 2 MiB limits, compact JSON plus newline, field
insertion order, tuple/path/dataclass conversion, legacy defaults, exact
unknown-field errors and PromptAdmission parsing. Preserve dispatch's optional
janitor_run_id omission, unheard-audio normalization, response ok/result/status
fallbacks, delivery and delivery_runtime provenance and F4 unknown behavior.
No changes to live fields, routes, socket paths, timeout/deadline/backoff,
Stop lease timing, environment, CLI/hook/skill interfaces or stored format.
Retain exception class identity and old imports for production and tests;
do not remove adapters merely because a static reference count becomes zero.

## Change rehearsal and evidence

Rehearsal: change the default/encoding rule for one existing private method
value in a disposable branch. Edit ONE contract descriptor/value owner; both
the existing client wrapper and server binding consume it without a second
method-name switch/decoder edit. Compare each side with independently specified
expected values, not just a round trip. Revert the deliberate wire variation;
the production compatibility batch changes no fields, defaults or bytes.

Ask A2 to sign the cases before any implementation: exact old/new command and
response corpus, malformed/unknown/version/size boundaries, missing/null/false/
zero handling, PromptAdmission invalid forms, old/new runtime delivery cases,
exception identity and cold import without state/DB/threads, known/unknown
method binding and capability/drain prelude ordering. Existing transport,
Stop/queue and dispatch characterization remain regression owners; new tests
must address a distinct boundary risk. Exercise codec/binding mutants, not a
snapshot of the new catalogue against itself. No such tests have run here.

After an accepted implementation, re-run the discovery import/backedge and
special-case scans into OWNED artifacts, never overwrite the accepted baseline.
Parameterize copies of analyse.py/supplement.py for the actual source/output
paths; their published versions hardcode refactor-untangle. Record source pin,
script hashes and targeted U09.1/.2/.3 observations separately from global SCC
or site totals. A smaller SCC or switch count alone is not proof of locality.
Run change rehearsal, G0/G1/own-base G2 and exact required source review.

## Sequence and outstanding coordination

A2 criteria READ before implementation: reviews/U09-U10-A2-acceptance.md
and reviews/U09-A2-wire-failure-matrix.md under the program directory. They
pin source a733 and independently specify the current behavior, UNEXECUTED.
The full matrix is binding, including encode trims/decode retains method
whitespace, equality-based version checks, ignored extra envelope keys,
unknown command fields before prompt parsing before missing text, truthy
string unheard-audio iteration, per-method error/falsey/default distinctions,
Stop sorting/omission and the existing four-method drain gate before decoding.
Do not harden surprising coercions. A new round trip alone is insufficient.
Cold import AND first parse must stay free of DB/execution dependencies.

Lead 681 resolves the prerequisite: accepted sweep-c extraction
1108803b32da49f70c4608cb4868d3b6c3c56f2f MUST be retained. Reviewed delta
f62c7bbc10242dbb54f70a57c76311926b8fc11a..1108803b, stable patch-id
bcc90a6eba501a1a0efbbfe1484a79c52fe99fb7, D SOURCE YES in
reviews/runtime-dispatch-extraction-1108803b32da.md. It is queued for merge17,
not yet on a733; the captain activates fwdport-watch.txt:19 for all five
methods when landing. Build U09 on its landed helper layout; do not copy,
undo or re-extract them. _dispatch_request is 142 lines there, with these
existing bindings:

| Wire method | Accepted same-class helper to retain |
| --- | --- |
| begin_stop | _begin_stop_request(self, params) |
| goal | _goal_request(self, params) |
| dispatch | _submit_dispatch_request(self, params) |
| finish_stop | _finish_stop_request(self, params) |

The codec descriptors bind to these existing executor methods. Retain the
capability prelude and outer dispatch_request lock/gate before binding
selection/effects. Bind other existing branches through the unified U09
boundary without rebuilding an independent name switch. This plan does not
repeat extraction gates: D's accepted evidence remains attributed to its exact
delta, and U09 will test its own changed codec/binding boundary on the actual
then-current source. HOLD any fresh sweep of these methods under 678. No owner
conflict remains after 681; exact post-landing base/watch readback is still
required before U09 code.

Docs branch starts from current integration. No code or landing before big
merge #1. After it ships, rebase onto current MAIN and land small batches
through the captain under 673b: values/compatibility first, then descriptor
consumers. No new integration branch/replay recipe mechanism after that point.
Coordinate runtime_bridge execution bindings with TP-02 owner rf-fix3c-7498;
U09 neither implements TP-02 nor borrows its in-principle behavior signature.
