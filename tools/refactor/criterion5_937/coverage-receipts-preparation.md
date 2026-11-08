# 937 shared receipt component — SOURCE ONLY, UNEXECUTED

B's e562 design disposition is ACCEPTED WITH AMENDMENTS, not SOURCE_READY or
execution permission. This slice implements only the shared in-memory receipt
boundary; no coverage import, data read/copy/update, SQL, report, source mapping,
process launch or outer runner exists in this delta.

`_run_receipts` now owns the prior timing consumer's unchanged collection,
worker/controller multiset, outcome/skip/nested, interval, identity-declaration
and raw-exit checks. Its mode is explicit. The timing wrapper keeps coverage
fields forbidden (now in BOTH start and finish), and returns raw timing fields
only in timing mode. ABBA explicitly refuses non-timing results. Failed pytest
results remain failed; zero exits/worker count also require exact int type,
rather than accepting bool as equal to0/8. There is no field-stripping projection
of a coverage run into timing and no execution-based compatibility claim.

The coverage wrapper consumes the complete unchanged starts/finishes, requiring
current exact B007 and declared coverage7.17/CTracer source paths. Start/finish
metadata and config hashes must agree. Each of nine distinct declared process
identities must supply a role-bound positive-byte-size/hash/name descriptor,
with no missing/duplicate path. Distinct roles with identical data hashes stay
nine candidate inputs; no content-hash deduplication. Actual early coverage
records come from the existing plugin; its code is unchanged in this slice.

The returned artifact state is deliberately DECLARED_ONLY. Metadata consistency
is NOT loaded-code/tool/tracer authenticity, actual kernel process identity,
positive process exit/connection close, bytes/inode custody, coverage schema,
legitimate-empty acceptance, or exact union. A positive declared byte length
is not a valid SQLite database. All such checks remain mandatory before any
library API or accepted aggregation. The component neither opens a source
artifact nor infers zero coverage from missing/empty files. Coverage output
omits instrumented elapsed/per-node metrics entirely and states
process_identity_custody_close_and_data_validated=False. It cannot serve as a
completed coverage report, measured no-worse result, or runtime-child proof.

Finite consumer cases are authored only: extend the existing shared fault table
across timing/coverage instead of copying a second synthetic owner; new cases
check every role retained with identical hashes, missing/wrong artifact paths,
hash/type/zero-size/tool/early metadata/typed exit/drift refusal, mode separation
without mutating receipts, and coverage exclusion from ABBA. Their independently
specified contract protects partial-worker/outcome acceptance and accidental
coverage-as-timing/data acceptance. These are LOWER PURE RECEIPT consumer cases,
not actual pytest/API producer/flush/valid-empty/close or native participation
controls. No production seams, copied native provider, actual SQL data or
manufactured passing database is introduced. No baseline RED/new GREEN claim.

Remaining B amendments are explicit integration obligations: closed-producer
original custody and no-sidecar/private-copy per-edge checks; strict public API
existing-format/valid-empty semantics and read mutation policy; exact contexts/
negative arcs/tracers and independent union; warnings/remapping/config/source
inventory/complete denominator and22-hotspot unique lineage; every library
connection's actual success/error/control close and primary preservation; actual
pytest/worker/kernel/source/flush boundaries and finite API controls; full source
archive/ordinary hard-budget/priority/drain. Shared receipt logic is not a
shortcut around them. No native/client/work/oracle/resource requirement is met
by this slice.6000s/~17NOK inclusive/LOWEST behind all gates, noFUND/preempt/delay,
full B/C source qualification and fresh compute/captain gap still required.
The new selector/count/cost remains UNQUALIFIED inside the existing control
allowance; no new job/window/Actions benchmark or herald931 resume.
