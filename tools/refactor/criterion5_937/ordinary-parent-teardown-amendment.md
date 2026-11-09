# 937 native teardown source audit and pre-code amendment

PRECODE ONLY; no adapter or controls authored/executed. B3a2 direction accepted
WITH AMENDMENTS, report SHAecf693513abd3a99159c6cf8e148c6ebad3b763dc85ce90e8eda76e3446fe242.
Native wait/birth observer cannot yet imply teardown/writer completion. This
source audit addresses those prerequisites before adding a half certificate.
Cached actual pytest9.1.1/xdist3.8/execnet2.1.2 source hashes/ASTs/METADATA/RECORD
and actual pinned coverage7.17 source archive in companion facts. Cache RECORD
consistency is not independent locked-wheel/loaded-source/interpreter ABI proof.

## Concrete teardown and consumed-error gap

pytest.wrap_session calls sessionfinish, then Config._ensure_unconfigure.
The latter invokes pytest_unconfigure followed by cleanup_stack.close in nested
finally blocks. A hook-finished marker is not evidence that the whole cleanup
stack succeeded. Xdist WorkerInteractor sends workerfinished from sessionfinish,
so later cleanup can fail AFTER the controller has received that event.

Execnet WorkerGateway.executetask catches BaseException from remotely executed
pytest code; ordinary exceptions close the channel with remote error text and
return from the task. This channel transport does not itself impose the same
exception as a nonzero OS child exit. Native worker status/workerfinished alone
therefore does not establish successful complete pytest cleanup.

On controller receipt, WorkerController.process_from_remote sets _down True at
workerfinished. Its later Marker.END path calls channel._getremoteerror(), but
notifies errordown only when not _down. Thus a post-workerfinished remote error
can be read without the existing errordown qualification path. This is a static
source-supported gap, not an executed failure or claimed actual production bug.
Channel._getremoteerror pops the first stored RemoteError; waitclose calls the
same getter. Adding a new read after native END would consume different/empty
state and cannot reconstruct a remote error that was already consumed. Do not
certify cleanup from channel closure, callback return, integer0 or PID absence.

## Proposed narrow observation, BEFORE implementation/re-review

Existing public pytest_configure_node(node) executes in WorkerController.setup
before native remote_exec/channel.send/setcallback(process_from_remote, END).
It potentially permits a per-node observational process_from_remote binding
BEFORE the native bound callback is captured. Observe the exact original callback
and exact node.channel object, never substitute WorkerController/remote module,
change protocol or add a channel read/RPC/waitclose. No global class/OS patch.

Before delegating a native callback, a narrowly source-bound same-channel
_getremoteerror observer could be installed/verified. Its original getter would
be invoked once PER NATIVE INVOCATION, returning the SAME actual error/None,
recording that result before native logic ignores/consumes it. It must not invoke
a getter itself, peek/replay/pop an error independently, suppress native calls or
create fake clean channel state. Missing channel, replaced binding, uncontrolled
concurrency, unobserved END/error path or callback/teardown completion stays
UNKNOWN/non-green. The initial live-gateway/Popen birth join remains separate.
A retained native remote error after workerfinished must poison the parent
qualified result even if unchanged xdist _down/native wait/controllerrc is0.

This is only a candidate source boundary, NOT accepted executable design yet.
The receiver callback runs on execnet threads and can itself catch BaseException;
ordinary diagnostic failures must leave native return/result behavior intact but
poison qualification. Metadata KI/SystemExit/control retention beyond native catch
layers, exact pending-primary/cleanup and before/after delegate policy need B
amendments before code. Do not blindly throw from inside a native catcher or
quietly drop a caught control. Native callback/error getter identity, scope,
reentry/history/inflight/restoration and return-assignment seams need bounded
explicit ownership. Restore only after actual callback completion; Group return
or a timed pool wait alone is not enough. No source approval carries from socket
or IO fault components into these native callback lifetimes.

## Native wait origin and writer boundaries still open

B's CPython3.12.14 primary-source caveat remains: _try_wait/_internal_poll may
synthesize0 for ECHILD, and Popen.wait can return cached status. This audit does
not authenticate a target stdlib or prove single native waiter ownership. A
same-object integer0 is not a fresh waitpid certificate. None/error/lost waiter/
autoreap/unsupported cached origin remains UNKNOWN. No global waitpid patch or
independent reaper/kill/newbackend is proposed to force a result.

Pinned coverage control source shows auto_save initially comes from auto_data,
and _atexit stops active measurement, may save (including sigterm path), and
closes deferred Data handles. The plugin saves in sessionfinish and uses its own
configured object. Those isolated facts do not bound every concrete forked child,
inherited instrumentation, collector thread or alternate writer of declared
raw/data paths. Full native coverage fork/producer source audit and actual output
writer ownership/closure still required. External coverage scope UNOBSERVED does
not waive an unknown writer; no positive writer-domain certificate is claimed.

The minimal future parent must join live native actor identities, qualified native
terminal origin AND complete error/cleanup/writer history to actual collector
terminal/final payload index, with separately qualified outer leader/fetch/
accounting observation AFTER payload exit. The payload cannot self-sign its
future parent exit. Existing c851 warning-only fetch is insufficient by itself.
Original nonzero pytest remains raw failure; raw0 plus any missing/error/unknown
closure/collector/index/fetch must qualified-nongreen. No accepted final index or
process/source/kernel/ABI flags True are authored here.

Strong future controls need actual pinned-xdist disposable owned children and a
real cleanup failure AFTER workerfinished, demonstrating original event order,
consumed native remote error and non-green parent qualification; not supplied
wait0/JSON. Existing lower lifecycle controls cannot simulate this capability.
Normal native completion and repeated/cached/None/concurrent waits, child death
before live capture, binding replacement, outstanding callbacks and metadata-
control exceptions need exact source selection + separate admitted setup. No
control authored here and no permission to run/probe/debug a new backend.

Whole6000s100min17NOK CPUheapdisk/IO fit still UNKNOWN: all new observer/cache/
source/dependency/hash/callback history/control costs join all old suite/native
corpus/API/graph/report/index/fsync/copy/fetch/drain/tails. Full B/C + faithful
wholefit + fresh normal LOWEST compute/captain gap behindALLgates precede ANY
execution. No blindtrial/shrink/capincrease/FUND/preempt/delay; if faithful useful
fit unjustified or >100min STOP/reportlead BEFOREcompute. Actions not937;
herald931 HOLD/rawfailures unchanged. All old controls UNEXECUTED; parent/writer/
sourceReadyFalse, no native API/import/parser/SQL/test/control/process/VM action.
