# Native readiness/identity observation — pre-code proposal, SOURCE ONLY

This document authorizes nothing. No client/observer/probe code is implemented
or executed by this slice. Request C criteria on the concrete source boundary
before authoring the native client wrapper. Exact per-pin native API/worker/log
source homes and hashes are in `readiness_api_sources.json`.

## Facts that prevent an invented ready flag

- Both pins expose native `RuntimeClient.status_within(total_s)` with a whole
  monotonic call deadline. Use the actual OWN-pin class/protocol/implementation,
  not copied wire encoding, a stand-in client or a different runtime source.
- Native HTTP `/status` emits200 even if its runtime block is unavailable.
  `RuntimeStatusView.block` catches native fetch errors and emits explicit
  unavailable/unknown fields. HTTP200 alone is not joint Host/runtime readiness.
- HTTP's runtime block projects availability/release/fence/drain/activity and
  observed_at, but does not export the runtime's random `instance`. A release
  string, including development/embedded, is not a unique process binding.
- Runtime native status reports an `instance` and optional self-attestation.
  `self_report` requires the clarp-runtime systemd cgroup and invocation shape.
  The proposed exact private Popen environment has no INVOCATION_ID and does
  not install/manage a runtime unit. Thus attestation may legitimately be None.
  Never forge environment, cgroup or self-report data to make it present.
- Host starts BOOT workers, performs native restart recovery, then starts
  SERVING workers before its native serve loop. WorkerSet.start records
  launched handles, NOT completion of their asynchronous work. Cache warm-up
  deliberately waits1.5s, then logs each completed step or a failed step.
  Serving, worker launch, cache completion and corpus catch-up are distinct.

## Proposed narrow runtime response binding

One separately owned sequential observer process per own-pin cell uses its
qualified private environment/source/guard and actual native RuntimeClient.
It issues only the already declared status calls to the exact owned Unix
socket, using the existing status_within API and unchanged frozen deadlines.
No extra RPC/nonce, alternate protocol, recovery/import call or retry is added.

Proposed observation: scope a wrapper on this ONE client's existing bound
`_connect` method, delegate once with identical arguments, and read Linux
SO_PEERCRED from the SAME successfully returned socket. Record bounded raw
pid/uid/gid plus the native result/instance and timing; return the original
socket to the original `_request` with its identity unchanged. Native internal
EAGAIN retries remain native; failed connects do not invent a successful peer.
Require exactly one successful returned-socket observation per successful
public status call. No socket/cursor proxy or additional connection is created.

The parent must retain the real root Popen birth/boot/source/launch and validate
its owned PID/start identity before and after the observation. Peer PID must
match that live owned runtime, in the same PID namespace. Socket literal path,
private ownership and physical identity must remain bound, not inferred from a
JSON pid. The native instance must remain stable across the declared bracketing
observations. This does NOT attest an unrelated manager, cgroup, descendant or
all-actor drain. No generic platform/cgroup capability is introduced.

Metadata failure makes the observation sticky UNKNOWN, preserving native call,
return and primary error. It must not create retry, change arguments, replace a
native exception or silently qualify a missing peer. Scoped binding restoration
must preserve whether the instance originally had its own attribute versus a
class descriptor, including on failure/concurrency rejection. No global
RuntimeClient or socket monkeypatch is proposed. Only this sequential observer
owns the client; unexpected concurrent use refuses proof.

Critical control-exception case for C: a control exception between successful
native connect and handing the socket to native `_request` must not swallow
KeyboardInterrupt/SystemExit, leak that owned socket or replace its primary
with diagnostic/cleanup failure. The implementation/control policy needs exact
review before this wrapper is authored. Ordinary getsockopt/recorder errors may
latch UNKNOWN while preserving the original native call; cancellation still
must propagate under the reviewed priority/cleanup rule.

All observed overhead belongs in timing/accounting; no guessed subtraction.
Native per-operation HTTP750ms and client500ms remain unchanged. A failed outer
HTTP deadline does not cancel/reap its native handler; outstanding work/drain
still needs a positive bound/receipt. No public ready endpoint is invented.

## Host response and required-work boundary remains incomplete

The owned Host's actual Popen/listener/loaded-code binding is still necessary;
matching a release string to the runtime is insufficient. HTTP's TCP socket
does not supply the same Unix SO_PEERCRED witness. Do not claim the proposed
runtime wrapper also certifies HTTP listener ownership. Existing root's actual
serve-entry object/endpoint observation is only partial evidence; parent/owned
listener identity and source/config/client endpoint binding still need design.

Preserve separate timestamps for first source/instance-bound positive runtime
response, Host HTTP responsiveness with explicit runtime available, completed
native worker stages, required corpus work/catch-up/quiescence and steady start.
No weak predicate may become the protocol's full-ready result. No marker is
fabricated from elapsed waits or a successful response. Required work cannot be
forced by manual import, recovery/reconcile call, grace/timeout/cooldown change,
worker disablement or a substituted index/checkpoint/source clock.

Potential passive evidence is the already-owned native stderr stream:
`log` emits a line before structured eventlog forwarding; cacheWarmup emits a
named step only after that native step returns, while failures have a distinct
event. It is a proposal to consume these actual source-bound records, not to
call the worker, replace logging, or assert line arrival is exact native event
time. Full bytes, ordering, stream ownership, lost/truncated/backlogged delivery
and monotonic receipt brackets/observer cost need qualification. Native error/
fallback classification must be preregistered, not chosen after outcomes.

## Finite criteria requested before implementation/admission

Use actual native client source with bounded synthetic sockets on buildbox only
when the FULL package/selector and compute route are admitted; none run now.
Cover wrong/missing peer, wrong pid/start/boot/instance, source/path mismatch,
malformed/native unavailable result, HTTP200+unavailable, native primary plus
metadata error, cancellation in the returned-socket handoff, unchanged socket/
return identity and exact instance attribute restoration, and concurrent use.
Attestation=None is retained truthfully and cannot be upgraded to manager proof.

These controls do not replace native positive root/work/guard/loaded-code/drain
proof. No new control job, sourceReady/activation, Actions measurement fallback,
provider/network/security expansion or backend qualification campaign is sought.
The same6000s/~17NOK inclusive/LOWEST-every-gate/captain-compute conditions and
herald931 platform hold remain in force. If faithful qualification cannot fit,
STOP/report that concrete gap before any compute instead of thinning the work.
