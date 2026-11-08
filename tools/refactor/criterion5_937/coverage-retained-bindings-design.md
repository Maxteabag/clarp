# 937 retained bindings owner — PRE-CODE source design, UNEXECUTED

B exact1b767a00 scoped source approval consumed, report
criterion5-937-1b767a00-B-union-graph-source.md SHA256
c57dd9051a94e1fd3293f7c613f4740b3f1f26f529b6285bc4b87ea9830985e7.
This proposal changes no executable, profile, caller, control or artifact.
Review the exact retained-byte boundary before implementing the next composition.

## Problem and resulting owner interface

build_union_with_graph currently receives parsed starts/finishes and binding
DIGEST DECLARATIONS. It correctly keeps their authentication false. Matching a
hash in a graph to a caller's claim does not prove retained bytes exist or that
the parsed records supplied to native union came from those bytes.

Proposed next owner accepts exact retained artifact observations/paths plus actual
coverage-class/union roots/capture inputs/limits and retained outer-exit/cohort
inputs. It does NOT accept alternative caller config_bytes/starts/finishes maps.
It reads the actual retained configuration and all18 start/finish JSON files,
decodes these same bytes strictly and passes ONLY those resulting objects to
existing build_union_with_graph. Existing full receipt/capture/actual raw-version/
API/reference/closed-aggregate validation remains intact. No weaker reducer or
field sanitization is introduced. Cohort/outer exit and execution/ledger identity
still require their separately qualified actual runner provenance; no claim that
reading a JSON assertion establishes a process or successful run.

## Retained inputs and fixed index contract

Four mandatory source-bound binding artifacts:

- Source catalog: exact current B007 coverage-source-B.json bytes, expected SHA
  c060901c910043552254a72d5c727646a84ac77bbca58a7bc61d10ea685d9f19.
- Coverage source archive: exact pinned coverage7.17/cp312 archive bytes, expected
  SHA49d58df8284f40cb16b781d5fc8b1d4fdefe185ce0c691e39e4b73d1f83c575b.
- Run configuration: exact actual --c5-config artifact bytes. Its computed SHA
  must match every original start/finish config_sha256 and graph binding.
- Run receipt index: exact retained typed JSON artifact whose computed SHA is
  run_receipts_sha256 in the graph. This is an index of actual retained records,
  not a repacked/canonicalized replacement for their original bytes.

Proposed index has EXACT keys format, pin, run_id, config_sha256, roles. Format
is criterion5-937-run-receipt-index-v1; pin is exactB007. roles has every fixed
controller,gw0..gw7 key exactly once. Each role has exactly start and finish, each
one the actual retained observation with EXACT path/dev/ino/bytes/mtime_ns/
ctime_ns/sha256 keys. Paths are canonical absolute physical paths; do not resolve
or normalize foreign aliases into validity. Digest strings are lowercase64hex;
all physical numeric fields are exact ints with appropriate nonnegative/positive
domains, NOT bool. All18 path and (dev,ino) identities must be distinct. Preserve
all18 native JSON artifacts even if bytes/hashes happen to match; no hash dedup.
The index is a binding artifact whose generation and exclusive final publication
must be owned by the actual runner/receipt collector, with retained observations,
not created from unverified success flags inside this consumer. That producer is
still unimplemented and not silently authorized by this design.

The current plugin source emits role.start.json in pytest_configure and
role.finish.json in pytest_sessionfinish, using JSON sort_keys/ensure_ascii and
one trailing newline. Both clocks and report durations legitimately include
floats. Retain original raw bytes and every field; do NOT rewrite their spelling,
whitespace, record fields, types or timestamps to make comparisons pass. Source
facts are retained separately from runtime behavior. Publication/control/loaded
plugin/closed-process provenance is still unqualified.

## Actual byte read and parser policy before native union

For each binding and each indexed raw receipt, require a private immutable physical
parent, owned regular single-link nonempty file, O_NOFOLLOW and expected observed
version/length/hash. No hardlink/symlink or stale-output fallback. Use existing
_root/_snapshot/_retain and exact comparator ownership patterns. A narrow retained
byte reader must own its known raw FD across read/close, recheck fstat and exact
count/growth, independently attempt close once, and propagate every pending
control-bearing outcome BEFORE optional post-snapshot/hash/decode/next input.
Pre/post sidecar/version/root checks remain conditional on exclusive namespaces.
FD-return/unknown close/remote containment remain qualified outer obligations.
No unguarded read_bytes/context-manager cleanup may reintroduce snapshot C1.

Raw input bytes and graph JSON have different supported types. The graph wire
stays unchanged (no floats). Receipt JSON must reject duplicate keys, nonfinite
NaN/Infinity, unsupported values and wrong top-level shapes. Permit only exact
JSON primitive types with finite floats where the ACTUAL receipt schema allows
them (monotonic stamps/report durations); do not allow a number to stand in for
a bool/int identity. The existing full reducer enforces its field contracts.
Source-ground any additional strict checks, rather than guessing a new producer
schema. No encoder/coercion equality, float rounding or generic bool==int match.
Because native union receives ONLY the objects decoded from retained bytes, no
second in-memory receipt map is compared using a weaker equality function.

Caller-specific per-artifact and aggregate byte/file/node/depth/name limits must
be frozen before file/API effects. Parsing/materialization/recursion, repeated
snapshot/read/hash and all original/decoded objects need numeric peak/CPU/disk
bounds BEFORE admission. A capped input or post-parse refusal is NOT a heap bound
or useful package-fit argument. Keep failure UNKNOWN and raw partials; no omitted
records, cap inflation, corpus shrink or blind trial. Source/tool archive need
hash/version binding, not extraction/import/execution by this owner.

## Composition and final linkage

After reading, require index pin/run/config bindings, exact raw decoded worker
identities/role names and start/finish pairing; then use the existing full reducer
and actual union to bind all18 observed raw/copy coverage inputs to per-role graph
captures. Frozen source/tool hashes come from actual read bytes. Computed config
and index hashes come from actual read bytes, never passed success strings.

Persist full captures/reference/closed aggregate through existing graph owner.
Retain a complete physical binding-observation record (all four binding artifacts,
all18 raw receipts and all original/copy data observations) in the final evidence
index. Recheck all binding/raw receipt versions after required union/publication
cleanup and BEFORE any report/index acceptance; changes mean UNKNOWN, preserve,
no accepted no-context report or next phase. Existing graph wire envelope need
not be broadened if its hashes link to this complete retained evidence index.
If an extra reference is necessary, propose the exact format change for review.
No hash-without-retained-bytes or in-memory pointer substitutes.

Byte matching may be reported as an actual observation at that narrow tier; it
must NOT silently set source_config_tool_run_binding_bytes_authenticated true
as a proxy for actual loaded module/source/interpreter/CTracer/process custody.
The latter remains false until the full source-bound runner's independent kernel/
process/native-class closure qualifies. No self-signed live identity is created.

Final report/index owner is still UNWRITTEN. show_contexts remainsTrue until exact
false-profile/report-consumer/region+outside-presence/SAME434 graph artifact
composition is reviewed; no no-context report or full SOURCE_READY follows.
Report index must be published LAST after required cleanup and full link checks.

## Finite controls and source review scope

Amend SAME9 API STORAGE control with actually retained SYNTHETIC config/start/
finish files/index, actual frozen catalog/archive fixture bytes and the byte-bound
owner. Its metadata remains synthetic, not real9 pytest/kernel proof. Preserve
all existing9update/order/literal-arcs/negative-only/empty-relation/original-byte/
actual graph-reopen checks. Existing raw records are retained, never replaced by
fabricated successful authentication fields. No extra native campaign requested.

Extend existing lower record/adapter table for duplicate raw keys, wrong role/
phase/config/index linkage, bool/int identity, nonfinite number, missing receipt,
changed retained version, read/close control priority and no next-input/report
continuation. Actual publisher/index generation fault controls belong to its
real owner; no source-shaped fake success. All cases remain UNEXECUTED and need
exact source qualification plus admission. Do not infer new selectors or jobs.

Full nativeRoot/work/raworacle/actors/lifetimes/8192-512fit/prehashbias/physical
restore/drain/wholeordinary runner/22hotspot mapping and all resource costs remain
OPEN. 6000s/100VMmin/~17NOK inclusive ALLsetup/control/copy/fetch/drain/tails, LOWEST
behind every gate/no FUND/preemption/delay/full exactB-C plus fresh compute/captain
gap before ANY execution. If useful faithful fit cannot be justified STOP/report
lead BEFORE compute. Actions program gates NOT937 admission; herald931 HOLD.
