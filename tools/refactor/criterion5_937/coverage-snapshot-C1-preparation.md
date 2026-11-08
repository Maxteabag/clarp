# 937 snapshot cleanup C1 — SOURCE repair, UNEXECUTED

B exactea3b finding FULLREAD, report
criterion5-937-ea3b31cb-B-graph-publication-source.md SHA256
abb967044285cf35dc155433d989177e11424bfc073132b2a5f94c4584826cea.
Prior publisher is NOT source-cleared; this repair requires exact B re-review.

The shared _snapshot used buffered fdopen context-manager cleanup without the
control-retaining selector. Read KeyboardInterrupt/SystemExit plus ordinary close
failure could export an ordinary primary, consumable by except Exception.
This is independent of immutable namespace/FD-return residuals and must be fixed.

Only _snapshot changes in coverage_copy_read.py. It owns one known raw descriptor,
uses os.read with the same chunk/exact-size-plus-growth probe and hash accumulation,
catches body BaseException and closes once in finally through existing _retain.
Control-primary identity wins with cleanup retained. Ordinary-primary/control-close
exports control-bearing BaseExceptionGroup retaining both original objects.
Ordinary/ordinary keeps original primary and its cleanup cause. No post-read
lstat, final hash, sidecar pass or returned snapshot is attempted after failure.
All prior sidecar, ownership/regular/single-link/size ceiling, opened identity,
pre/open/post descriptor/path byte/version fields and returned digest remain.
There is no retry, removal, cap increase or numeric descriptor fallback.

Avoiding fdopen also avoids introducing a stream-constructor ownership-transfer
state. The os.open return/assignment and unknown close lifetime still need outer
containment. Successful raw os.read can return short chunks, continuing until
EOF/exact growth probe as before; actual syscall/cost timing must be requalified,
not inferred equal from the content/hash invariants. No resource fit claimed.

## Shared source impact

Snapshot callers in coverage_copy_read, coverage_build_union, coverage_render and
coverage_graph_publish now all reach this repaired shared ownership path. Their
own source/ASTs are unchanged, but previous helper-byte carries do NOT certify
this new composition. Request shared-helper impact review explicitly. All other
functions in coverage_copy_read, including _root, _retain, _same_capture and the
read/capture/update owner, are AST-identical to parent. No product source changes.
Report projection remains True; no publisher/index/report wiring added here.

## Fault adapter tier and value

Three LOWER lifecycle cases call the ACTUAL _snapshot with controlled tool-module
OS/hash bindings in an isolated child with no concurrent helper users. Only this
tool module's bindings change; global OS/product/API objects are not patched.
Adapter state is unconditionally restored. The old fdopen shape is supported so
an eventual exact old/new proof reaches the genuine cleanup-selection boundary.
No baseline failure/current pass is claimed without admitted execution receipts.

Cases: control read+ordinary close; ordinary read+control close; ordinary/ordinary.
Require both exact original failures retained through selected primary/cause/group,
control-bearing outcomes not consumed by except Exception, one open/read/close,
only initial lstat/sidecar queries, zero optional hexdigest/next-phase work.
This observable error-export/lifecycle contract is distinct from earlier outer
API-read close-priority cases and from actual filesystem/wire storage cases;
those do not inject faults inside snapshot's known-descriptor owner. A regression
using unguarded context-manager cleanup or exporting ordinary cleanup would fail.
No production seam or manufactured descriptor/actual filesystem proof is added.

The opaque adapter descriptor is NOT a real FD and the faults are not real signals
or kernel close behavior. Actual publisher fault containment, same434 full graph
artifact composition and source/config/producer/custody authentication remain OPEN.
These3 cases plus all previous controls are UNEXECUTED. No helper import/API/SQL/
control/test/root/socket/VM execution, only AST/Git/source-data checks.

No semantic graph/report/index acceptance or whole SOURCE_READY. Need exact B/C
package qualification, concrete graph/parser/hash/IO/serialization/control CPUheap/
disk/whole6000s fit and rf-computec/captain fresh LOWEST sourcebound gap before any
execution.6000s/100VMmin/~17NOK inclusive all setup/control/copy/fetch/drain/tails,
no FUND/preemption/delay; if useful faithful fit cannot be justified, STOP/report
lead before compute. Actions program gates NOT937 admission; herald931 HOLD.
