# Own-pin native root and normal-flow source preparation

Source-only draft; no root/control/VM execution or runnable/admitted campaign.
No production source edit, provider/data access or new process capability.
C764's three capacity domains remain binding; this slice is NOT their proof.

## Native root composition draft

`native_root.py` is a function-only composition, no command-line activation. A
fresh private process invokes the selected pin's ACTUAL server.py/runtime.py as
__main__, with the same real Python (sys.executable unchanged). It checks private
environment, physical source/cache/link inventory and a fresh external child
prefix, then installs the source-bound private Guard before ANY lib imports.
It imports NO initial clock module and installs NO clock hook. All startup
invariants/workers/recovery remain native; no warm-up/import/grace/queue skip.
Each pin uses its own runtime source, not the old fixed-A runtime.

The helper explicitly uses runpy and a temporary native script argv; it is not
claimed byte-for-byte launch equivalence to a bare direct script invocation.
That contract and every actual native launch form need exact review/control.

A narrowly scoped BaseServer.serve_forever wrapper captures the REAL object,
checks the expected owned endpoint and delegates original args/return/primary
once. Serve entry is diagnostic ONLY, not readiness. The independent actual RPC
and HTTP readiness/source/instance/work validation still needs implementation.
Diagnostic write failure marks unknown but does not replace the qualified native
serve call. Native thread exceptions are counted without retaining traceback
objects; the original thread exception hook still runs once. The wrapper does
not yet observe native errors swallowed by product code: full flow/log/transaction
classification and capture remain required.

The owner's bounded stdin pipe accepts only an exact attempt/PID/start-bound
STOP record; it calls shutdown on the captured actual server. Bad input, EOF,
cooperative deadline or unfinished helper joins remain partial. The helper does
NOT signal arbitrary processes, retry a root, clear queues, shorten native
timeouts or install an executable/child_env bridge. Source checks and cooperative
control are NOT a hard timeout/containment/drain route. Shutdown can itself
block; qualified outer supervision with ownership and absolute deadline is owed.
Native-return diagnostics are always qualified=false. Observing live threads or
root return is not positive thread/child completion. The one helper thread join
has at most1s inside the absolute deadline; every other actor/drain requirement
still needs source-bound owner proof. Unknown roots remain preserved/unreused.

## Instrumentation/resource caveats before any metric qualification

The current draft rehashes the full product tree before entry and retains the
input file map. That warms pages, costs CPU/I/O and can retain different helper
heap footprints for the two source trees. It is NOT negligible/zero overhead or
qualified native cold-disk/steady-RSS evidence. Resolve a common source/package/
loaded-code policy and quantify/bound all helper costs before measurements; no
guessed subtraction or instrumentation difference manufactured as product gap.
The primary parent spawn-to-positive-ready interval remains separate from helper
entry/serve observations. Dynamic coverage, immutable dependency/helper closure,
config/ancillary seed/source/DB identity and actor exception/epoch banks remain
unwired. A guard eligibility declaration does not certify private source or state.

## Normal-flow proposal fixed before outcomes

`native_flow_profile.json` binds five rule candidates to separate exact A/B
source and AST identities. It is NOT an executing exception classifier.

- _WriteNeeded: actual unlocked native probe control before forwarding a write,
  caught by its real full/tail importer then a locked pass. Preserve transaction
  history; do not treat all exceptions as probe control or lose previous commits.
- Admission polling: exact source plain SQLITE_BUSY / _only_acquires_lock path,
  not arbitrary OperationalError or extended code. Preserve original native
  polls, cursor finalization and result; add no retry/fetch/proxy.
- Whole-operation retry_locked: bind its actual caller/retries argument and ALL
  attempts/earlier commits. Default3 is not a grant for uninspected overrides;
  a later return does not excuse an unrelated failure or unknown acknowledgement.
- Native tail invalidation: B has the source-classified fallback candidates; A's
  exact whole-server source has no _TailInvalidated/_try_tail/_refuse_tail names.
  Do not manufacture a common A fast-tail profile or require B-only alias objects
  in A. All native fallback work/counters/raw outcomes and capacity belong in the
  declared treatment. This profile is not permission to ignore a missing CP.
- Approved usage ENOENT: match EACH pin's exact source callsite, native Popen
  argv/kwargs/cwd/immutable absent namespace and actual errno/filename. No fake
  ENOENT, generic missing-program waiver, returned provider or old cert carry.

Unmatched paths/failures remain PARTIAL/STOP before any metric acceptance. The
current finite Witness does not implement this full source/classification or
all-process proof; it must not be wired as if it did. Missing/ambiguous fields
and absent aliases remain visible, never guessed from outcomes.

## Focused controls to author/qualify before execution

Preserve actual serve delegation/return/primary on metadata failure; endpoint or
source mismatch refuses; captured server isn't readiness; controller STOP/EOF/
malformed/oversize/deadline/join and replaced-hook cases; native guard/thread
exceptions cannot disappear; memory/source provenance cannot be inferred from
JSON true flags. Actual native roots/actors and source-qualified normal flow
need their own bounded remote cases and full raw work oracle. No synthetic
server stand-in may be labelled actual root/source/boot coverage. Existing
terminal, source/clock, native transaction and packed-storage controls remain
unexecuted. No full suite, new capture, measurement or retry is requested here.

## F1/F2 source repair after C7f review (controls not executed)

Ordinary observer/helper failures are caught as Exception. Process-control
BaseExceptions leave serve publication/handoff directly and do not proceed to
original serve; native signal handlers are unchanged. During finalization only,
a control exception is temporarily held to attempt independent binding restores,
then re-raised. If an original primary is already unwinding, its exact object
survives with the finalization/control exception as explicit cause. No further
diagnostic I/O is initiated while a control exception is pending. This is not
an all-signals/backend/drain certificate; missing terminal evidence is UNKNOWN.

Pre-entry metadata is now entrypoint-intent.json, explicitly ROOT_NOT_STARTED.
Owner-helper start, entry preparation, intent publication, delegated entry and
return/exception-metadata phases are separate. Only the delegated runpy call's
exception can populate the native-primary candidate. A bounded target-path
traceback walk distinguishes entered exceptions from driver/load UNKNOWN;
filename evidence alone is NOT loaded-code/source provenance. Inspection or
post-return observation failure cannot turn into native-primary. The temporary
returned namespace is released rather than retaining product actors/globals.

The diagnostic record includes launch_phase, native_call_attempted,
native_entry_state, failure_origin, native_completion, native/tool/driver and
finalization error types, and restoration_ok separately. Normal native return
and SystemExit(None/0) conventions are explicit, but neither is metric or actor
success. Owner-start and entry-publication counterexamples must prove zero
runpy invocations and ROOT_NOT_STARTED/tool origin. Cancellation publication
must prove zero delegated serves; ordinary publication OSError must still prove
one exact delegated serve. Native error plus finalization failure must preserve
the original primary identity and separate cause. All C7f finite controls remain
owed/unexecuted; no source finding is self-closed and no permission is inferred.

## Pending-control finalization residual repair (after C7b)

F2 was source-closed on7b; C identified a remaining F1 path when a control was
already propagating before final_error was initialized. The new control_pending
latch spans original unhandled control primary, retained secondary, cleanup,
restoration and final publication. It is never reset on entry to finally.
SystemExit(None/0) explicitly accepted as normal native completion remains a
separate case, not an unhandled control path.

While latched, no new owner join/is_alive check, guard-file probe, thread census
or root-return write/fsync begins. Only the necessary in-memory done notification
and independent required binding restores are attempted, then exception
propagation. The restoration descriptors are prepared before native execution;
observer installation is in the protected tool-setup phase. No signal handler
or masking policy changes. The original primary identity is retained; the first
retained secondary control cause takes precedence over a later ordinary cleanup
failure. New control during cleanup/restore also gates every subsequent optional
probe/publication. An absent terminal file is UNKNOWN, not successful cleanup.

This is source repair only. Distinct finite cases for pending primary, retained
secondary during diagnosis, newly arriving finalization/restore control and
ordinary metadata failure still need authored/reviewed/admitted proof. Actual
actor/process exit, durable refusal retention, source/RSS policy, hard backend
and the entire measurement package remain unqualified. C must close the source
finding; no execution or source-readiness permission is inferred here.
