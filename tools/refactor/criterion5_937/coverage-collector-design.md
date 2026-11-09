# 937 retained coverage collector and index: pre-code proposal

Status: SOURCE PREPARATION / METHOD REVIEW REQUESTED. No collector, process
observer, index publication, controls or execution is implemented by this change.
Exact antecedent dad42d39 B scoped SOURCE YES is consumed, report SHA256
1804343a0fd0f414e8f012cefb8c32e9127d4df4e7d0a8385bec24fb148d972a.
Source facts and exact hook/reducer/index/owner AST hashes are retained in
coverage-collector-source-facts.json. Those facts are source inspection only.

## Two different index boundaries

The existing run-receipt-index-v1 is an artifact-address index: format, B pin,
run_id, actual config SHA and all nine exact start/finish file observations.
It is needed BEFORE the union/report consumer reads retained records. It is
not the final accepted graph/report index and cannot attest actual process
exit, closed writers, loaded class, or supported coverage denominator.

The final graph/report index is needed AFTER all consumer/API/descriptor
cleanup and source/data/raw/graph/report version checks. It must also bind the
ordinary compute runner's actual accounting/cancellation/drain evidence and
measurement qualifications. That index is still unwritten. This proposal does
not replace it with a renamed early receipt index or a successful Python return.

## What the current producer actually records

The plugin publishes controller/gw0..gw7 start and finish records to fixed paths.
Start includes self-read PID/start/boot/interpreter/affinity/config metadata.
Finish stops/saves coverage and reads the data path for hash/size, then publishes
its terminal record from pytest_sessionfinish. pytest_unconfigure executes
later and stops the coverage object again. The finish hook does not explicitly
close every CoverageData connection or observe its own process exit. No closed
writer/drain claim can be inferred from save, finish existence, unit status,
controller completion, or a PID that happens to be absent at one later instant.
Existing publication uses temporary-file fsync, hardlink, unlink and directory
fsync. Its error/control ownership and partial-name lifecycle still need exact
qualification; the collector must not silently bless it by copying the record.

## Chosen next executable boundary, after method review

Implement a bounded POST-EXIT byte collector and early artifact-index publisher,
not a new arbitrary descendant supervisor. It owns retained files and its known
FDs only. The qualified ordinary parent/compute route must separately supply
actual source/launch/process/exit and writer-containment evidence before this
collector may be used as part of a measurement acceptance path. The source
package cannot manufacture that evidence from plugin JSON or caller True flags.
Absent such integration, a storage-only collector result remains conditional,
explicitly unauthenticated, and cannot admit the report/measurement package.

The parent contract must bind the actual Popen/controller PID-start-boot,
source archive/config/argv/interpreter/dependency/cache/CPU/machine identity,
and the native worker census to exactly those nine start+finish identities.
Worker births must be observed while alive and bound through the qualified
ownership route; later JSON claims alone do not reconstruct historical ancestry.
Actual controller wait/exit, bounded in-flight completion and owned descendant
closure come from that same route, including actor/namespace and SSH-loss
limitations. Do not invent a global cgroup/subreaper contract, use cancelled
soak machinery, or interpret this proposal as backend/probe permission.

1. Freeze per-file and aggregate raw/data/index/path/JSON byte bounds and whole
   resource/time accounting BEFORE collection. The actual immutable run plan
   supplies them; no MAX constant is an allocation or fit certificate. Preserve
   independent writer/exit evidence separately from metadata declarations.
2. After external writer closure is positively qualified, open only the exact
   private owned output root. No glob-based acceptance, worker replacement,
   wildcard delete, live DB, checkpoint/healing, coverage combine, native import,
   or data-file reopening through CoverageData in this collector. Inventory and
   preserve unexpected/partial files as diagnostics; a missing/malformed/unknown
   required artifact refuses the candidate, not an excuse to omit it.
3. Observe actual immutable catalog, archive, profile, config, all18 fixed raw
   records and nine native data paths with a finite raw-FD read/hash owner.
   Check regular/singlelink/no-follow/UID/parent and descriptor epochs, sidecar
   absence, bounded exact count/digest and independent known-close outcomes.
   Use streaming archive hashing and preserve original bytes/paths. New observed
   versions must be constructed from actual file observations, not supplied
   claimed hashes. Source/loaded-class authentication remains separate.
4. Strict-decode only those actual config and record bytes. Apply BOTH phase
   type-channel/schema binding, actual native coverage filename/PID mapping,
   full reducer/cohort/outcome/error census, plus observed data byte/hash equality.
   No parsed caller-map alternative, bool/int equality, finite-clock coercion,
   inferred missing finish, empty-file success or receipt sanitization. Retain
   nonzero original exits/failed raw data even when no usable index is published.
5. Build precisely run-receipt-index-v1 from the checked run/config and actual
   eighteen observations. No process-authenticated field is added. Check every
   observed input again after required descriptor cleanup and before publication;
   unknown closure/version means preserve/refuse/no next phase or retry.
6. Publish the early index at one fixed fresh name by exclusive creation under
   bound private root FD; finite write, file fsync, known-close with control-safe
   error retention, directory fsync, independent strict reopen/hash/typed equality.
   Any publication error leaves diagnostics, never returns a usable observation.
   A filesystem name may exist after interruption: consumers may use it ONLY
   with the successful separately retained parent/collector terminal evidence;
   file existence or decodability alone cannot certify publication completion.
   No error-path cleanup that erases uncertain evidence and no automatic retry.
7. Pass actual checked observations/read-derived records to the existing capture
   and bound union/report pipeline. The nine private data copies are a later
   distinct API owner: early index construction cannot counterfeit those captures
   or call complete41 preflight before the copies physically exist. Consumer
   preflight remains mandatory once actual copies/observations are available.

The early index cannot contain its own digest/version; that observation is
obtained only after close/reopen. Its observation is carried by the next outer
receipt, and finally by the accepted graph/report index after all required
cleanup. Do not create an infinite chain of self-signing manifests or pretend a
final hash independently proves successful fsync or process drain.

## Control ownership for subsequent source proposal

Use one actual storage fixture owner to publish raw files, invoke the collector
once and independently reopen its index, checking fixed role/path/hash/bytes
against original fixture bytes. It is STORAGE with SYNTHETIC process metadata,
not pytest/kernel/source/exit proof. Extend that owner for missing/wrong-role,
source/config/hash/version/data-sidecar/duplicate identity and pre-existing index
refusals. Keep complete originals and fail before downstream API/report work.
LOWER known-FD fault rows cover read+close/write+close/fsync+close and control
priority only where the storage owner cannot force genuine kernel failures.
Actual process exit/actor containment controls belong to the admitted ordinary
runner, not a fake True receipt. No tests/controls are authored or executed here.

Before implementation, B must review the exact scope, producer-vs-index split,
closed-input observation boundary and fixture/control ownership. Before any
execution the whole source package still needs B/C, concrete faithful inclusive
6000s/100VMmin/about17NOK CPU/heap/disk/IO fit and fresh compute/captain LOWEST
normal allowance/gap behind every program gate. Collection/publication/source
hashes/JSON/API copies/graph/report/fsync/fetch/drain/tails all count. No blind
trial, cap inflation or role/context/region/corpus/control/hotspot shrink. If
faithful useful fit cannot be justified or needs more than100min, STOP/report
lead before compute. Actions gate exception does not admit937; herald931 HOLD
and all historical raw failures remain unchanged. Final collector integration,
accepted index, nativeRoot/work/oracle/resources/physical restore/22-hotspot
historical mapping and complete ordinary runner remain OPEN.
